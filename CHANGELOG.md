# Changelog

All notable changes to the FM-1 Fleet Manager. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Entries are grouped by the audit
items they close (`docs/AUDIT-fleet-manager.md` §11). Hashes are on `phase-b/p1`.

## Phase B: unreleased

Phase B is the audit's repair work on top of `audit/oss-prep` (`cd36045`): the panel's
safety and layout, the write path, the web surface, the generalisation, and the fixtures.
It is not yet merged to `main`. The bench verification that remains is in
[`docs/RELEASE-CHECKLIST.md`](docs/RELEASE-CHECKLIST.md).

### Added

- **Things that need the internet fail gracefully when the Pi is offline** (on its own hotspot, or with no
  network). `hub.online()` is a cached connectivity check (no default route, or the hub unreachable within a
  few seconds); with it, the catalogue is served from the saved copy at once, marked offline with its age,
  instead of waiting out the 60 s timeout, and a download is refused immediately with a plain message
  (`hub.HubOffline`, HTTP 503 with `offline: true`) that says what still works and offers `fm1-switch import`.
  The admin page shows an **offline** badge and greys the Download buttons; the touch screen's GET FIRMWARE says
  so; `fm1-switch hub` and `fetch` print it. `FM1_OFFLINE=1` forces offline. Tests: `tests/test_offline.py`.
- **WiFi fallback: the Pi makes its own hotspot when no saved network connects** (bench test pending).
  `fm1_switch/netctl.py` (the only code that runs `nmcli`) and `fm1_switch/netwatch.py` (the rules and
  the loop), `fm1-switch net status|setup|passphrase|watch|up|down`, a *WiFi and hotspot* settings group,
  `systemd/fm1-net.service` and `polkit/50-fm1-wifi.rules` (neither installed by default). The hotspot
  is `FM-Pi` with a **published default passphrase** that the Pi makes you replace: the first phone to
  open any page on the hotspot is taken to a form (`fm1_switch/wifi_setup.py`) that makes it choose a passphrase
  and can reach nothing else until it does; the touch panel and the house network are never gated. The setting
  `net_default_passphrase` is `gate` (default), `refuse` (the hotspot will not come up with the default) or
  `allow`; `net status` and `net setup` say so, and `net passphrase --prompt` changes it from the Pi. It goes back to a saved network only when no phone is joined, and
  never creates or changes a saved network. Guide: `docs/GUIDE-wifi-hotspot.md`; bench test:
  `docs/BENCH-wifi-fallback.md`. Verified on a real Pi: the whole fallback from the service (loss, hotspot, a joined phone is never dropped, return), the
  first-use passphrase form, and the timed revert; plus a synthetic end-to-end test.
- **Automatic re-register after a power-up.** Turning units off and on re-enumerates all of them,
  and look-alike units can then only be told apart by port plus USB device number, so the panel
  asked for RE-REGISTER FLEET after every power cycle. The web server now runs it unprompted
  (`fm1_switch/autoreg.py`) once the bus has stayed unchanged for 20 s, only for the units that
  need it, never during a write or with a unit in download mode, at most twice per episode, and
  not for a lone new unit. Settings: *Re-register units after a power-up* and *Wait for the units
  to settle*. The result is logged as `auto: matched ...`. Verified on the bench 2026-11-27: after a
  power cycle it matched A, C and D (B was already proven) with no tap.
- **Oxi One and channel layouts** (dawless hub). A USB MIDI controller found by name (the Oxi One is
  built in, as `oxi`) can be a route's source or destination; it is left out of the master clock and
  of panic. Routes gain a `shift` (move channels up or down, `route add --shift N`), and **layouts**
  (`fm1_switch/layouts.py`) turn each unit's channel count and its own listening channel into
  routes: a `packed` and a `blocks` layout are always available, and custom ones are saved by name. The
  expert page has a Channel layout section (`/api/router/layouts*`), and `fm1-switch route layout
  list|show|apply|units` does the same from a shell. Applying writes an ordinary patch,
  `auto-<layout>`, and makes it active. Works for up to sixteen one-channel units (the unit cap
  went from 8 to 16). Verified on the bench 2026-11-27 with an Oxi One driving four units.
  Guide: `docs/GUIDE-midi-hub.md`.
- **Screenshots of the touch UI and the admin page, and a tour of the screens**
  (`docs/GUIDE-screens.md`, `docs/screenshots/`), captured from a real bench with unit ids masked.
- **Guides for registering units and for the MIDI hub** (`docs/GUIDE-registering-units.md`,
  `docs/GUIDE-midi-hub.md`), including what to do after the USB cables or a hub move.
- **RE-REGISTER FLEET: put the units back on their profiles after a reconnect.** When the cables move, the
  registry's saved ports and fingerprints go stale and running units that look alike (same
  firmware, same `bcdDevice`) cannot be told apart from USB: the letters show NOT CONNECTED
  and the units show as `?`. The fleet screen now offers RE-REGISTER FLEET (`POST /api/reconcile`,
  `guided.reconcile`) whenever a letter is missing and an unlettered unit is running, or a letter rests on a
  port guess. It puts every running unit into download mode (the button is shown only when
  something looks wrong), reads each one's flash id, matches it to its letter by id, restarts them and re-learns each one's port and fingerprint.
  A flash id nobody owns is reported, never bound (REGISTER stays deliberate), and nothing is
  written to any unit's flash. Look-alike units placed only by their last-seen port are
  drawn `A~` and count as unproven once the unit re-enumerates (the registry keeps the USB
  device number from the last match), so swapped cables cannot silently swap letters; RE-REGISTER
  FLEET is offered for those too. Tested against a fake bus; first bench run 2026-11-27.
- **Settings page on the web admin.** Hardcoded tunables now live in one schema
  (`fm1_switch/config.py`) and are edited at `/index.html` → Settings (`GET`/`POST
  /api/settings`): flash, backup, restore and wait timeouts, catalogue freshness and refresh
  cool-down, download size limit, automatic backups kept, starting tempo, flash-hold lifetime,
  touch device, and every folder and file. Values are read when used, so they apply without a
  restart (a few say "restart to apply"). Saves are all-or-nothing and audit-logged. The
  flasher program, the maker's installer folder, the hub address and the network/token
  settings are shown but not editable from the website, on purpose.
- **Master clock and transport** (dawless hub): `fm1-switch clock set|start|stop|targets`, a
  CLOCK screen on the ROUTES page, and `POST /api/router/clock`. The running router sends MIDI
  start, clock and stop to the chosen units from `~/fm1-clock.json`. A router always starts with
  the clock stopped. Tested against a fake sequencer only; not yet run on the units.
- **No screen needed on the Pi.** Everything the touch panel does, including the routes, sets
  and master-clock screens, is also served by headless mode (`fm1-switch web --headless`, port
  80) to a phone or tablet. The HDMI panel is now optional, not part of the appliance.
- **Register a unit from the panel** (B4, FEAT-01): SETTINGS → REGISTER scans for units in
  download mode, shows each flash id the server has just read, and binds the one you pick to
  a sticker letter in a two-step confirm. Refusals come with their reason on screen.
  `2d460d7`, `fc88492`, `7e6203b`.
- **Headless mode** (B16): `fm1-switch web --headless` serves the same touch UI on port 80
  for a phone or tablet on the LAN. LAN hosts are accepted by name or IP, wrong write tokens
  are throttled, and a portrait phone is told to turn sideways. `fa07cfc`.
- **Generalisation** (B13): `~/.config/fm1/config.json` sets the unit count (1–8), the paths
  and the m-flasher binary; environment variables still win. The panel grid widens above four
  units. `dbfa0eb`.
- **Backups from the panel** (B11): BACK UP NOW saves a copy by hand, and a DEL on a backup
  row opens a RESTORE / DELETE chooser. `6fa2a83`.
- **Firmware Hub maker credit** (B10): the panel names each firmware's maker and links its
  repository. `237a39e`.
- **Real fixtures** (B15): soft-key reply, `/dev/snd` and USB trees before and after, the
  hub catalogue, and `m-flasher info` on a new-chip (0xB36014) unit, all captured on the bench
  Pi and scrubbed of unit ids. `bee0b5b`, `59b8d1a`, `efb8e86`. Provenance is in
  `fm1-switch/tests/FIXTURES.md`.

### Changed

- **Panel polish** (B11): a 2 s poll on every view; "PI SERVICE NOT RESPONDING" after two
  failed polls; failed units get their own red state; package marks are words; SEVERAL ends
  with a per-unit result list; CANCEL is the larger target; DEL sits clear of its row;
  backup dates carry the year. `07047f5`, `891df69`, `f9b4dbd`, `6fa2a83`.
- **The root serves the panel's own touch UI** (fleet screen on `/`, so a phone sees what the
  HDMI screen shows); the hub and library page moves to `/expert`. `13dcd00`.
- **Expert page** (B12, UX-06): browses the hub and manages the library. It no longer lists
  or flashes units; flashing is the panel's job. `82960b4`.
- **Fleet screen layout** adapts to the screen: a portrait phone gets one scrolling column
  (two on a tablet); landscape scales to the tighter dimension; HDMI 800×480 is unchanged.
  `5427525`, `9631796`.
- **Status wording** (B9): a slow boot gets 60 s before it is called stuck; "not re-learned"
  no longer claims the unit failed to restart. `48592f3`.
- **Identity** (B5): the scan finds download-mode units through sysfs; a running unit that
  cannot be matched shows as `?`; guessed stickers are drawn with a `~`. `efd6fa8`.
- **Code structure** (B14): `web.py` is split into `web_state` (shared state and the
  catalogue), `web_writes` (every act that changes a unit or a backup) and `web_http` (the
  gates, the router and the endpoints). `web.py` keeps `serve()` and re-exports the old
  names. No behaviour changed; each moved definition is identical to its old form. Request
  gates and the token moved to `security.py`, and backup listing, space and pruning to
  `backups.py`, in earlier steps of the same item. `3f53668`, `5e2dde0`, `9c1306f`.

### Fixed

- **A held press cannot complete a write** (B1, UX-01): a confirm dialog ignores clicks for
  600 ms after it opens and ignores any click whose press did not start on its button; its
  actions row is placed away from the control that opened it. `47e014d`, `98c3e60`.
- **Concurrency** (B7, CONC-01, SAFE-03, R1–R7, PKG-01): no device read runs while a write
  does; the registry and inspection caches are locked; a second request no longer overwrites
  the running write's status. `e8fb37e`.
- **Write path** (B6, SAFE-02/04, GD-02/04, MF-01/03): the sha256 is required before a write;
  the backup is verified from its files; a failed write is logged as failed. `2c21e97`.
- **Audit log** (B8, SAFE-05): every refused request leaves a line with its stage; a line that
  could not be written shows in `/api/fleet` and on the panel. `4b90ca3`.
- **Stale panel renders** (B11, JS-01): an out-of-date async render stops itself, and library
  hashes are cached. `07047f5`.
- **Registry** (B15): a unit in the ROM loader is not also counted as a running unit.
  `59b8d1a`.
- **Bench m-flasher output** (B15): `info` parses the 0.12.1 output format. `bee0b5b`.

### Security

- **The API refuses requests that were not made by its own UI** (B2, SEC-01): Host and Origin
  checks, and a JSON content type on every mutation. `5e97b65`.
- **Mutations from other machines need a token** (SEC-01): `X-FM1-Token`, kept in a 0600 file.
  `9bdff0f`.
- **Loopback by default** (SEC-02): the LAN is an explicit opt-in (`--host 0.0.0.0` with
  `FM1_WEB_HOSTS`). `dde912c`.
- **Static serving is contained and allow-listed** (SEC-03): `GET /../web.py` no longer serves
  the source. `f591a20`.
- **State files are owner-only** (SEC-05): the flash log, registry and backup images are
  created with umask 0077. `b7428ec`.
- **Scrub before publication:** the bench's real flash ids became obviously fake fixtures, and
  the systemd units, `calibrate.py` and package metadata carry placeholder identity.
  `e75abf3`, `aae98da`, `096f1e5`.

### Removed

- **`fleetctl`** (FC-01): the port-addressed writer and its package are retired; one package,
  one writer, one rule file. `70dec0a`.
- **`fm1-switch install`** (SAFE-01). `8454e61`.
- **The console fleet unit and its font file** (B13). `dbfa0eb`.

### Tests and docs

- Python: 266 tests pass (from 170 at the start of phase B). JS: 57 pass for
  `tests/js/fleet.test.mjs` (from 34).
- `tests/FIXTURES.md` says which fixtures are captured and which are synthetic. `5e2dde0`,
  `efd6fa8`.
- Gate and p1 reports: `docs/REPORT-phase-b-gate.md`, `docs/REPORT-phase-b-p1.md`.
- Phase D roadmap for firmwares that need their maker's installer:
  `docs/ROADMAP-installer-firmwares.md` (`b7bee0f`). Not part of phase B's scope.
- Release checklist for the bench: [`docs/RELEASE-CHECKLIST.md`](docs/RELEASE-CHECKLIST.md).
