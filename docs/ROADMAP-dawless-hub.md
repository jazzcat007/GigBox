# Roadmap item: the Pi as a USB MIDI host and dawless hub

**Status (updated 2026-11-25):** the core router is built and runs on the bench Pi (routing
core, ALSA sequencer client, systemd unit, ROUTES page on the touch UI, panic, `fm1-switch route`).
Verified so far: all four units attach, a live route A to B passes messages byte for byte, and
the soft key still works while the router holds the units. The rest of the functions below are
ideas. Every claim here is **UNVERIFIED** unless it says otherwise.

## The idea

The units are plugged into the Pi for firmware management. The same connection can make the Pi the
centre of a dawless rig: a USB MIDI host that routes between units, and a small always-on box that
does the jobs a computer normally does. A class-compliant USB MIDI device needs no cooperation
from its firmware, so this works for open-source and closed firmwares alike.

## Side benefit: per-unit channels without touching the units

Remapping channels on the Pi (A = channels 1–4, B = 5–8 …) gives each unit its own range without
writing any setting to a unit. That removes the unverified firmware writes from the critical path
of [ROADMAP-device-settings.md](ROADMAP-device-settings.md) for any firmware, including ones with
no MIDI configuration. Firmware-side settings stay useful for rigs that run without the Pi.

## Functions, grouped

### Routing (core)

- **Routing matrix:** any unit's output to any unit's input, saved as named patches, recalled from
  the touch screen or a phone.
- **Merge and split:** several sources into one input, one source to several inputs.
- **Channel remap and filter:** per route, change or block channels, notes, CC numbers, program
  changes, clock, and SysEx.
- **Transpose, velocity curve and note-range split or layer** per route.
- **Panic:** one button sends all-notes-off and resets on every port. This also clears hung notes.

### Timing

- **Master clock:** a tempo, start and stop that every unit follows, with swing and clock divide or
  multiply per output. Run it from the Pi with no DAW.
- **Clock follower:** follow a unit or an external DIN or USB clock and redistribute it.
- **Tap tempo and transport** on the touch screen.
- **Latency and jitter check:** measure the router's added delay before relying on it live.

### Performance

- **Monitor:** a live view of MIDI traffic per port, to see what a unit is sending.
- **Arpeggiator, chord and scale-quantise** as per-route processors.
- **Sequencer, looper and phrase recorder:** record MIDI from any port and play it back, with
  standard MIDI file import and export.
- **Scenes:** a button that changes routes, tempo and per-unit program changes together.
- **Program change and bank manager:** one name per sound across the rig.
- **Controller mapping:** a USB or Bluetooth controller, or the touch screen, mapped to CCs on any
  unit.

### Connectivity

- **Network MIDI (RTP-MIDI, Apple-compatible):** a phone, tablet or laptop sees the units over
  Wi-Fi with no cable.
- **Bluetooth LE MIDI** in and out.
- **Web MIDI from the phone UI**, so the touch UI can play and control units.
- **DIN or TRS MIDI** through a USB interface for gear without USB.
- **CV or gate clock out** through an add-on, only if wanted.

### Audio (optional, needs an audio interface)

- **Recorder:** record the rig's audio to a file on the Pi, or to a USB drive.
- **Click and metronome** and a cue mix.
- **Simple mixer or USB audio routing** for class-compliant audio devices.
- Not a goal: a DAW, plug-in hosting or a sampler. The point is to stay dawless.

### Rig management

- **Backup, versioning and restore of the whole rig:** firmware, backups, routes, scenes.
- **Project and set lists:** a named set of firmware per unit, routes and scenes, loaded in one
  step, with the existing flash and backup safeguards.
- **Unit health:** which units are present, which firmware each runs, power draw and USB errors.
- **Hotplug:** when a unit is replugged or powers on, its routes come back on their own.
- **Remote access** over the home network only. Nothing internet-facing by default.

## Design constraints (these protect the firmware work)

- The router must **release a unit's port before download mode or a flash**, and resume after.
  The soft-key SysEx and the flash path stay the top priority, and routing never blocks them.
- **The Pi becomes the single point of failure for routing.** Units must still work directly if
  the Pi is off. Routing only adds.
- **Power:** several USB devices on the Pi need a powered hub. Measure it before relying on it.
- **Same touch UI** on the HDMI screen and in the headless phone UI. The hub needs no screen on
  the Pi: routing, the clock and panic are all reachable from the phone page (`fm1-switch route`
  and `fm1-switch clock` work from a shell too). The expert page stays at
  `/expert`.
- **Keep the device layer separate** from routing, as in the multi-device goal, so DrumBoy Pro,
  Akai and other devices can join later.
- Prefer existing parts (ALSA sequencer, `aconnect`, `rtpmidid`, a small router service) over
  writing a MIDI stack from scratch.

## Survey, 2026-11-25 (read-only, on the Pi; nothing written to a unit)

**VERIFIED on the bench:**

- **All four units enumerate as USB MIDI ports in normal running mode.** `amidi -l` lists four
  ports (`hw:0,0,0` to `hw:3,0,0`) and `aconnect -l` lists four ALSA sequencer clients. The kernel
  binds each through `snd-usb-audio`, so they are standard class-compliant devices. The Pi needs
  no driver for routing.
- **Port names do not identify a unit.** They read "Mutable Dreams FM-1", "FM-1", "Felucca" and
  "Felucca". Two share a name, and the names do not match the firmwares the fleet manager
  reports (grimm, ghoulbox, jiant and a fourth). All four share USB id `1209:0001`
  (pid.codes test PID). A router must therefore key each port to a **USB port path and the
  registered flash id**, never to the name or the card number. Card numbers can change on replug.
- **The Pi is a Raspberry Pi 3 Model B+.** All four units sit behind its on-board hub, which
  is shared with Ethernet. The units are full-speed (12 Mbit/s), so bandwidth is no concern for
  MIDI.
- **Under-voltage has happened since boot.** `vcgencmd get_throttled` returned `0xd0000`: under-voltage,
  throttling and a soft temperature limit have each occurred. None is happening now (the low
  bits are clear). It is consistent with four units drawing from the Pi's own ports, so a
  **powered hub is needed before routing is relied on**.
- **Tools present:** `amidi` and `aconnect`. `rtpmidid` (network MIDI) is not installed.

**Still UNVERIFIED:**

1. Whether each firmware sends or accepts notes, clock and CC over USB the way a router would use
   them. Not tried: listening opens the port, so it waits until it can be done without risk to
   the soft key.
2. Measured round-trip latency and jitter through the Pi for one hop.
3. Whether the soft key still reaches a unit while ALSA holds its port open.
4. Which cause the under-voltage has, and whether a powered hub clears it.

## Built so far (2026-11-25)

**Routing core, pure logic: `fm1_switch/router.py`, tests in `tests/test_router.py`.** No ALSA, no
unit contact, nothing written to hardware. It covers: a `Route` that filters by channel and kind,
remaps the channel, transposes, scales velocity and limits the note range; a validated `Patch`;
a `PatchStore` (`~/fm1-patches.json`, atomic write, broken files refused and left alone); panic
messages; `ports_to_release` for the rule to free a unit's routes before a flash; and the
letter-to-channel-range split. Ports are named by registry key, not by card number or port name.
**The ALSA service (2026-11-25), also with tests; checked on the Pi against ALSA's own Midi Through
client only, never a unit:**

- `fm1_switch/alsa_seq.py`: a sequencer client over `ctypes` and the Pi's `libasound.so.2`, so no
  new dependency (`dependencies = []` stays). One port, subscribed both ways to each unit, so
  letting go really frees the unit's raw MIDI device. ALSA's own codec handles running status;
  `SysexAssembler` joins SysEx that arrives in several events. **Verified on the Pi:**
  `python alsa_seq.py loopback` sent a note-on, a program change, a CC and a SysEx to Midi Through
  and read all four back byte for byte.
- `fm1_switch/router_service.py`: runs a patch. Attaches only the units the patch names, re-resolves
  them every 2 s (client numbers change on replug), and logs a missing unit instead of failing.
- **Flash release is cross-process.** The soft key now asks a running router to let go of every unit
  first (a hold file, `~/fm1-router-holds.json`; the router reports in `~/fm1-router-status.json`).
  With no router running it writes nothing and waits for nothing. A hold always expires (60 s for the
  soft key), and a router fault never blocks the soft key. The hold is fleet-wide because the soft key
  has been seen to reach more than the unit it was aimed at.
- CLI: `fm1-switch route list | show | add | delete | run NAME | status | panic`.

**Bench trial, 2026-11-25, on the four real units (no flash write).** Deployed to the Pi first
(code backed up to `~/fm1/code-backups/`).

- **VERIFIED. Router hop latency, virtual clients only (the USB leg is not in it):** 500 messages
  through Midi Through, the router and a second client: 0 lost, median 0.32 ms, 95th percentile
  0.50 ms, 99th 0.72 ms, worst 1.55 ms. That is Python plus ALSA on the Pi 3B+. The cost of USB
  to a real unit is on top and was not measured.
- **VERIFIED. All four units attach.** The registry resolver mapped A..D to their ALSA clients
  (28, 24, 20, 16); `route run` attached all four and `route status` listed them.
- **VERIFIED. Sending works.** The panic set (192 messages, CC 120/121/123 on every channel) went to all
  four with no error. The units did not respond audibly in a way I could observe, as the trial did
  not listen to audio.
- **VERIFIED. The soft key works while the router holds the units.** With the router attached to all four,
  `fm1-switch download fm1-d` released every unit, wrote the soft key, and unit D reported download
  mode (`4c4a:8057`). A, B and C stayed running this time. The router took the units back when the hold
  ended, and unit D's vanishing logged one harmless "no such device". Unit D was restarted with
  `m-flasher restart` and the router re-attached it by itself. This answers open point 3.
- **VERIFIED. A live route, A -> B (second run, 75 s).** Unit A runs grimm, a drone firmware, so it
  sends notes on its own (channel 3, note-on and note-off). The router received 21 messages from A
  and sent 21 to B, byte for byte the same (no remap). C and D sent nothing. No MIDI clock byte came
  from any unit. Whether B sounded was not confirmed, as B's channel is unknown.
- **Not verified.** Round-trip latency to a unit (the USB leg), whether B reacts to what it is sent,
  and whether the soft key works after hours of attached routing.

**systemd unit (2026-11-25): `fm1-switch/systemd/fm1-router.service`, installed and enabled on the Pi.**
A *user* unit like `fm1-web` (the Pi has lingering on, and no sudo is needed). `fm1-switch route run`
now takes an optional name (default `default`, or `FM1_PATCH`, or `"patch"` in the config file), idles
while that patch does not exist, and follows edits to `~/fm1-patches.json` without a restart.
**Verified on the Pi:** it started idle, picked up a `default` patch within 2 s and attached A and B,
let go when the patch was deleted, stopped and restarted cleanly (the status file goes on stop), and
is enabled at boot. Idle cost: about 3 % of one core and 21 MB. **No patch is left on the Pi, so
nothing is routed until you make one.** Found on the way: the resolver first used `registry.known()`,
which runs `m-flasher info` on every unit in download mode; on a 2 s timer that could touch a unit being
flashed. It now uses `registry.running_slots()`, which reads sysfs only (tested).

**Fixed during the trial:** a crash (segfault) when ALSA reported an error, from a pointer
truncated by ctypes' default return type; stop on SIGTERM and SIGINT so units are released (a background
shell ignores SIGINT); an already-vanished unit counts as released.

**Routes page (2026-11-25): ROUTES on the touch UI** (HDMI panel, and the same page on a phone at
port 80). It lists the saved patches with the router's state (running or not, units live, not found,
freed for a flash), opens a patch to show its routes, adds a route (FROM unit, TO unit, and under
OPTIONS: send on one channel, transpose by octaves, block clock, block CC), removes a route, deletes a
patch, runs a patch or stops routing, and sends PANIC (all sound off on every running unit).
Server side: `web_router.py`, `GET /api/router` and `POST /api/router/{route,route/delete,patch/delete,activate,panic}`,
behind the same Host, Origin, JSON and token gates as every write. **Nothing here writes to a unit's
flash.** Choosing the patch writes `~/fm1-router-active.json`; `route run` with no name follows it
(`PatchFollower`), so RUN and STOP take effect within the router's 2 s rescan; a name given on the
command line still wins. Edits that change what plays or lose work (RUN, STOP, DELETE, remove a route,
PANIC) use the panel's armed confirm dialog; adding a route only edits a file and does not. PANIC
refuses to run while a flash is running. Found and fixed on the way: `route panic` used to overwrite a
running router's status file; a one-shot panic now never writes it. Range, velocity and a channel
filter are still CLI only. **Verified in the browser at 800x480 and phone width, and by tests (Python
and JS); deployed to the Pi on :8080 (the port-80 phone service needs `sudo systemctl restart
fm1-web-headless`). Not exercised on the Pi: PANIC and RUN (they send MIDI to the units).**

**Master clock and transport (2026-11-25, built and tested; NOT yet run on the bench).**
`fm1_switch/clock.py` is the timing core (24 pulses per quarter note on a fixed grid, so a late
poll does not drift; a stall of more than 8 ticks drops the backlog instead of bursting).
Settings live in `~/fm1-clock.json` (`bpm` 20 to 300, `running`, `targets`; no targets means every
running unit). The running router follows the file like it follows the active patch, attaches the
targets (even if no route names them), sends MIDI start, then clock ticks from its own loop (ALSA
stays single-threaded), and sends stop before letting a unit go: on STOP, when a unit leaves the
targets, and when the router exits. A soft key or flash still frees every unit first, and a
released unit simply gets no clock. **A router that starts always starts with the clock stopped**,
so a reboot never sets the units' sequencers running. Control: `fm1-switch clock [status] | set BPM
| start | stop | targets [UNIT ...]`; `POST /api/router/clock` (starting needs `confirm: "clock"`);
and a CLOCK screen on the ROUTES page (tempo ±1/±10, target units, START with a confirm, STOP).
Note: a route that forwards clock from one unit to another **and** the master clock would double
the ticks; block clock on such routes (`--block clock`). **Unverified on hardware:** that the units
follow the clock, tick jitter over USB, and whether a unit ignores start/stop. Tap tempo, swing and
divide/multiply are not built.

**Not yet done:**

1. Check by ear that B plays what A sends (match the channel with `--remap`).
2. Run the master clock on the bench (a firmware that follows MIDI clock), measure its jitter, and
   do the powered-hub check.

## Suggested order

1. Finish the survey: the four unverified points above, starting with a powered hub.
2. A routing core: matrix, channel remap and filter, panic, and the rule to release ports around
   flashing. Add a touch screen page and a phone page.
3. Master clock and transport.
4. Network MIDI and Bluetooth MIDI.
5. Monitor, scenes and set lists.
6. Sequencer and looper, then the audio options, only if they are still wanted.

## Acceptance (for the first build)

- Four units route to each other from the touch screen and from a phone, saved and recalled by
  name.
- Flashing a unit from the panel still works with routes active, and routes resume afterwards.
- Channel remap gives each sticker letter its own range with no write to any unit.
- With the Pi powered off, the units still work directly.
- Added latency is measured and written down.
