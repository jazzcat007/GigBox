"""``fm1-switch`` — CLI and web UI entry point (the panel shows /fleet via cog).

    fm1-switch web             serve the expert web UI on loopback (the panel's path;
                               --host 0.0.0.0 opts in to the LAN, with a write token)
    fm1-switch list            what the Pi can see, no UI (good over SSH)
    fm1-switch import          copy packages from removable media into the library
    fm1-switch hub [QUERY]     search the Firmware Hub catalogue from the shell
    fm1-switch fetch ID        download one catalogue package, verified, into the library
    fm1-switch inspect FILE    explain what a .fwsc carries and how safe it is to install
    fm1-switch download SLOT   put one unit into download mode via the soft key
    fm1-switch diagnose        check m-flasher, sg, udev symlinks

There is no install/flash command here on purpose: a CLI write to a port-named device
skips the flash-id match, the backup, the lock and the audit log (audit SAFE-01). The
panel and /api/flash -- the only writers -- go through guided.flash, which enforces all
of them. Exit codes: 0 ok, 1 failed, 2 bad usage.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from . import library, mflasher, registry, softkey, units
from .units import SLOTS, SLOT_LABELS, enumerate_units

USAGE = """usage:
  fm1-switch web [--host H] [--port N] [--headless]
                                expert web UI, on 127.0.0.1 by default; --host 0.0.0.0
                                opts in to the LAN, and off-Pi writes then need a token
  fm1-switch list               list units and packages (no UI)
  fm1-switch import             copy .fwsc from removable media into the library
  fm1-switch hub [query]        search the Firmware Hub catalogue
  fm1-switch fetch <id>         download one catalogue package (sha256 verified)
  fm1-switch inspect <file>     what a .fwsc carries, and whether m-flasher completes it
  fm1-switch diagnose           check m-flasher, sg driver, udev symlinks
  fm1-switch download <slot>    put ONE unit into download mode (soft key, by USB port)
  fm1-switch units              what the Pi thinks each unit is, and how sure it is
  fm1-switch register <slot>    learn a unit's sticker by having it power on alone
  fm1-switch route list|show NAME|delete NAME|status
  fm1-switch route add NAME SRC DEST [--channels 1,2] [--remap N] [--transpose N]
                                [--block cc,clock] [--velocity X] [--range LO-HI]
                                add a one-way MIDI route between two units to a saved patch
  fm1-switch route run [NAME]   run a patch (the dawless hub router; default patch name
                                "default"); follows edits to the patch file; Ctrl-C stops it
  fm1-switch route panic        send all-sound-off to every running unit
  fm1-switch set list|show NAME|delete NAME|check NAME [A,B]
  fm1-switch set save NAME A=PACKAGE B=@backup ...   a rig set: one firmware (or the newest
                                saved image, @backup) per unit, 1-4 units. Pushing a set
                                is done from the SETS page or the API (one flash lock).
  fm1-switch asset sample build NAME OUT.fsmp FILE.wav[:ROOT[:LO-HI]] ...
                                WAV files -> a sample slot image, on the host (nothing sent)
  fm1-switch asset sample info FILE.fsmp
                                read a slot image back: zones, key ranges, CRC
  fm1-switch asset pattern import|export|syx|show ...
                                MIDI file <-> pattern JSON; pattern -> TRACK_STEP .syx

slots: fm1-a fm1-b fm1-c fm1-d
The library is ~/fm1-firmwares (override with FM1_LIBRARY).
"""


def cmd_list() -> int:
    units = enumerate_units()
    if units:
        for u in units:
            print(f"{u.slot:<7} {u.device}")
    else:
        print("(no units in download mode)")
    pkgs = library.library()
    print()
    if pkgs:
        for p in pkgs:
            print(f"{p.label}")
    else:
        print(f"(no packages in {library.library_dir()})")
    return 0


def cmd_import() -> int:
    staged = library.staged_packages()
    if not staged:
        print("(nothing to import: no .fwsc on removable media)", file=sys.stderr)
        return 1
    for pkg in staged:
        try:
            dest = library.import_package(pkg)
        except FileExistsError:
            print(f"skip {pkg.name}: already in the library")
        else:
            print(f"imported {dest.name}")
    return 0


def cmd_web(args: list[str]) -> int:
    from . import web

    port = None
    headless = False
    # Loopback by default (audit SEC-02): the panel talks to this machine, and reaching
    # the API from the Wi-Fi is an explicit opt-in that also turns on the write token.
    host = "127.0.0.1"
    i = 0
    while i < len(args):
        if args[i] == "--port" and i + 1 < len(args):
            try:
                port = int(args[i + 1])
            except ValueError:
                print(f"bad port {args[i + 1]!r}", file=sys.stderr)
                return 2
            i += 2
            continue
        if args[i] == "--headless":
            # No panel: serve /fleet to phones and tablets on the LAN (port 80 unless
            # --port is given). Implies a LAN bind, so the write token is switched on.
            headless = True
            i += 1
            continue
        if args[i] == "--host" and i + 1 < len(args):
            host = args[i + 1]
            i += 2
            continue
        print(f"unknown option {args[i]!r}\n\n{USAGE}", file=sys.stderr)
        return 2
    if headless and host == "127.0.0.1":
        host = "0.0.0.0"
    if port is None:
        port = 80 if headless else 8080
    return web.serve(host=host, port=port)


def cmd_hub(args: list[str]) -> int:
    from . import hub

    query = " ".join(args)
    try:
        cat = hub.load()
    except hub.HubUnavailable as exc:
        print(exc, file=sys.stderr)
        return 1
    hits = cat.search(query)
    hits.sort(key=lambda e: (-e.stars, e.name.lower()))
    for e in hits[:60]:
        flags = []
        if e.beta:
            flags.append("beta")
        if e.chip == "new":
            flags.append("new-chip")
        elif e.chip == "old":
            flags.append("old-chip-only")
        if not e.installable_by_us:
            flags.append("maker's installer")
        print(f"{e.id:<16} {e.name[:26]:<26} v{e.version[:12]:<12} "
              f"{e.ident:<14} {','.join(flags)}")
    print(f"\n{len(hits)} of {len(cat.entries)} firmwares match"
          f"{f' ({cat.updated})' if cat.updated else ''}")
    if cat.from_cache:
        print(f"(from the cached catalogue, {cat.age_seconds / 3600:.1f} h old — "
              "the hub asks that it be read at most once a day)")
    return 0


def cmd_fetch(args: list[str]) -> int:
    from . import hub

    if len(args) != 1:
        print("fetch needs one catalogue id (see `fm1-switch hub`)", file=sys.stderr)
        return 2
    try:
        cat = hub.load()
    except hub.HubUnavailable as exc:
        print(exc, file=sys.stderr)
        return 1
    entry = cat.by_id(args[0])
    if entry is None:
        print(f"no firmware with id {args[0]!r} in the catalogue", file=sys.stderr)
        return 1
    try:
        path = hub.fetch_package(entry, library.library_dir())
    except hub.HubError as exc:
        print(f"failed: {exc}", file=sys.stderr)
        return 1
    print(f"{entry.name} {entry.version} -> {path}")
    print(f"sha256 verified against the catalogue ({entry.sha256[:16]}…)")
    from . import packages

    ins = packages.inspect(path)
    print(f"{ins.severity.upper()}: {ins.headline}")
    for detail in ins.details:
        print(f"  · {detail}")
    return 0


def cmd_inspect(args: list[str]) -> int:
    from . import packages

    if len(args) != 1:
        print("inspect needs one .fwsc file", file=sys.stderr)
        return 2
    path = Path(args[0]).expanduser()
    if not path.is_file():
        print(f"no such file: {path}", file=sys.stderr)
        return 1
    ins = packages.inspect(path)
    print(f"{path.name}: {ins.severity.upper()} — {ins.headline}")
    for detail in ins.details:
        print(f"  · {detail}")
    return 0 if ins.severity == "ok" else 1


def cmd_download(args: list[str]) -> int:
    """Send the soft key to exactly one unit, named by slot, via its physical USB port."""
    if len(args) != 1 or args[0] not in SLOTS:
        print(f"usage: fm1-switch download <{'|'.join(SLOTS)}>", file=sys.stderr)
        return 2
    slot = args[0]
    # Where every unit is right now, read from the bus before anything is sent. Taken once,
    # before the soft key: afterwards the other units may already be in download mode.
    ports = registry.live_ports()
    port = ports.get(slot)
    if not port:
        print(f"{slot} is not on any USB port the registry can place right now. Check that "
              "it is switched on and its cable is connected. A unit is placed by its firmware "
              "fingerprint, so two units with the same fingerprint cannot be told apart.",
              file=sys.stderr)
        return 1

    current = softkey.port_ids(port)
    if current is None:
        print(f"nothing is plugged into {port} — is {slot}'s cable connected and the unit "
              "switched on?", file=sys.stderr)
        return 1
    if current in softkey.DOWNLOAD_IDS:
        # Already where we want it. Idempotent, and NOT an error.
        print(f"{slot} is already in download mode on {port} ({current})")
        print(f"  addressable as /dev/{slot}" if Path(f"/dev/{slot}").exists()
              else f"  ⚠ no /dev/{slot} yet — check the udev rule")
        return 0

    print(f"{slot} is on USB port {port}; sending the soft key…")
    try:
        res = softkey.soft_key(port)
    except softkey.NoSuchPort:
        print(f"nothing is plugged into {port} (is {slot}'s cable connected and the unit "
              "switched on?)", file=sys.stderr)
        return 1
    except softkey.NoMidiInterface as exc:
        print(f"{exc}", file=sys.stderr)
        return 1
    print(f"  wrote the soft key to {res.midi_node}")

    if softkey.wait_for_download_mode(port, timeout=20.0):
        print(f"  ✔ {slot} is in download mode ({port} now reports "
              f"{softkey.port_ids(port)})")
        # Warn honestly: on this hardware one soft-key write has been observed to move
        # EVERY MIDI-capable unit into download mode, not just the addressed one.
        others = []
        for other in SLOTS:
            if other == slot:
                continue
            other_port = ports.get(other)
            if other_port and softkey.port_ids(other_port) in softkey.DOWNLOAD_IDS:
                others.append(other)
        if others:
            print(f"  ⚠ {', '.join(others)} ALSO entered download mode — the soft key "
                  "reached more than the addressed unit (it has been seen to do so)")
        else:
            print("  note: the soft key may reach other MIDI-capable units too — "
                  "re-read them rather than assuming")
        if Path(f"/dev/{slot}").exists():
            print(f"  {slot} addressable as /dev/{slot}")
        else:
            print(f"  ⚠ no /dev/{slot} yet — check `udevadm info -q path -n /dev/sg0` "
                  "against the rule in fm1-switch/udev/80-fm1.rules")
        return 0

    print(f"  ✘ {slot} did not enter download mode within 20 s "
          f"(port still reports {softkey.port_ids(port)}).\n"
          "    The soft key writes nothing, so the unit is unharmed. Try power-cycling "
          "it to let the bootloader fall back to ROM boot.", file=sys.stderr)
    return 1


def cmd_units() -> int:
    """Show the fleet as the Pi currently understands it, and label the certainty."""
    from . import registry

    reg = registry.load()
    print(f"registry: {registry.registry_path()}"
          f"{'' if registry.registry_path().exists() else '  (none yet)'}")
    print()
    print(f"{'slot':<7} {'state':<16} {'unit id':<20} {'chip':<10} firmware")
    for u in registry.known():
        if u.in_download_mode:
            state = "download"
        elif u.running and u.provisional:
            state = "running (~)"
        elif u.running:
            state = "running"
        elif u.present:
            state = "on USB"
        else:
            state = "off/unplugged"
        slot = u.slot or "-"
        print(f"{slot:<7} {state:<16} {u.unit_id:<20} {str(u.chip or ''):<10} "
              f"{u.firmware or ''}")
    print()
    print("Running units on the bus:")
    running = registry.running()
    if not running:
        print("  (none)")
    for r in running:
        hint = f"  -> likely {SLOT_LABELS.get(r.slot_hint, r.slot_hint)}" if r.slot_hint else ""
        midi = "MIDI" if r.has_midi else "no MIDI"
        print(f"  {r.port:<10} {r.vendor_product:<10} fp={r.fingerprint or '?':<6} "
              f"{midi:<8} {r.product or ''}{hint}")
    free = reg.free_slots()
    print()
    if free:
        print(f"unlabelled slots: {', '.join(SLOT_LABELS[s] for s in free)}"
              "  ->  fm1-switch register <slot>")
    provisional = [s for s, b in reg.units.items() if b.provisional]
    if provisional:
        print(f"provisional (confirm by reading flash): "
              f"{', '.join(SLOT_LABELS[s] for s in provisional)}")
    return 0


def cmd_register(args: list[str]) -> int:
    """Learn one unit's sticker by having it be the only one to power on."""
    from . import registration

    if len(args) != 1 or args[0] not in SLOTS:
        print(f"usage: fm1-switch register <{'|'.join(SLOTS)}>", file=sys.stderr)
        return 2
    slot = args[0]
    print(f"Labelling the unit that wears sticker {SLOT_LABELS[slot]}.")
    for line in registration.guidance():
        print(f"  · {line}")
    print()
    before = registration.snapshot()
    if before:
        print(f"{len(before)} unit(s) are already on: "
              f"{', '.join(sorted(before))}")
        print("Switch off every OTHER unit before continuing.")
    input("Press Enter when only the unit to be labelled is about to be switched on… ")
    print(f"Watching for exactly one new unit (up to 120 s)…", flush=True)
    try:
        result = registration.wait_for_arrival(before, timeout=120.0)
    except registration.AttributionError as exc:
        print(f"\nRefused: {exc}", file=sys.stderr)
        return 1
    except TimeoutError:
        print("\nNothing appeared. Switch the unit on and run this again.", file=sys.stderr)
        return 1
    print(f"  saw {registration.describe_arrival(result)}")
    try:
        registration.register(result, slot)
    except (registration.AttributionError, OSError) as exc:
        print(f"Could not record it: {exc}", file=sys.stderr)
        return 1
    print(f"  recorded {SLOT_LABELS[slot]} provisionally.")
    print("  To confirm, put that unit in download mode, then REGISTER it on the panel")
    print("  (SETTINGS -> REGISTER): the Pi reads its flash id in download mode and")
    print("  upgrades this provisional label to a real one.")
    return 0


def cmd_diagnose() -> int:
    problems = 0
    exe = mflasher.binary()
    usable = os.path.isfile(exe) and os.access(exe, os.X_OK)
    print(f"m-flasher: {exe if usable else exe + '  NOT FOUND or not executable'}")
    if not usable:
        problems += 1

    try:
        with open("/proc/modules") as fh:
            sg = any(line.startswith("sg ") for line in fh)
    except OSError:
        sg = None
    print(f"sg driver: {'loaded' if sg else 'NOT loaded' if sg is False else 'unknown'}"
          + ("" if sg else "   (sudo modprobe sg)"))
    if sg is False:
        problems += 1

    units = enumerate_units()
    print(f"units in download mode: {len(units)}")
    for u in units:
        print(f"  {SLOT_LABELS.get(u.slot, u.slot)}  {u.slot} -> {u.device}")

    lib = library.library_dir()
    print(f"library: {lib} ({len(library.library())} packages)")
    return 0 if problems == 0 else 1


def cmd_set(args: list[str]) -> int:
    """Rig sets from the shell. Saving, listing and checking write only ``~/fm1-sets.json``.
    Pushing is deliberately not here: every unit write goes through the web server, which
    holds the one flash lock; a second process could collide with a panel write."""
    from . import rigsets, web_sets
    from .guided import WriteRefused
    from .units import SLOT_LABELS

    usage = ("usage: fm1-switch set list|show NAME|delete NAME|check NAME [A,B]|"
             "save NAME A=PACKAGE|@backup ...")
    if not args:
        print(usage, file=sys.stderr)
        return 2
    sub, rest = args[0], args[1:]
    letter = {v: k for k, v in SLOT_LABELS.items()}
    try:
        store = rigsets.SetStore.load()
        if sub == "list":
            for name, rs in sorted(store.sets.items()):
                print(f"{name}  ({', '.join(SLOT_LABELS[m.slot] for m in sorted(rs.members, key=lambda m: m.slot))})")
            if not store.sets:
                print(f"no sets yet ({store.path}); make one with: fm1-switch set save NAME A=PACKAGE ...")
            return 0
        if sub == "show" and len(rest) == 1:
            rs = store.sets.get(rest[0])
            if rs is None:
                raise rigsets.SetError(f"no set named {rest[0]!r}")
            for m in sorted(rs.members, key=lambda m: m.slot):
                print(f"{SLOT_LABELS[m.slot]}  {m.label}")
            return 0
        if sub == "delete" and len(rest) == 1:
            store.delete(rest[0])
            print(f"deleted {rest[0]}")
            return 0
        if sub == "check" and 1 <= len(rest) <= 2:
            only = None
            if len(rest) == 2:
                only = [letter.get(x.strip().upper()) or "" for x in rest[1].split(",")]
            for st in web_sets.preflight(rest[0], only):
                note = st["blocking"] and f"BLOCKED: {st['blocking']}" or st["warning"] or "ready"
                print(f"{SLOT_LABELS[st['slot']]}  {st['label']}  {note}")
            return 0
        if sub == "save" and len(rest) >= 2:
            picks = []
            for item in rest[1:]:
                who, _, what = item.partition("=")
                slot = letter.get(who.strip().upper())
                if not slot or not what:
                    raise rigsets.SetError(f"{item!r}: write it as A=PACKAGE or A=@backup")
                picks.append({"slot": slot, "backup": "newest"} if what == "@backup"
                             else {"slot": slot, "package": what})
            saved = web_sets.save_set(rest[0], "", picks)
            print(f"{saved['name']}: {len(saved['members'])} unit(s) saved")
            return 0
    except (rigsets.SetError, WriteRefused) as e:
        print(f"set: {e}", file=sys.stderr)
        return 1
    print(usage, file=sys.stderr)
    return 2


def cmd_route(args: list[str]) -> int:
    """The dawless hub's patches and router, from the shell. No unit is written to: routing
    only passes MIDI, and the service lets go of every unit before a soft key (router_service)."""
    from . import router, router_service

    usage = "usage: fm1-switch route list|show NAME|delete NAME|add NAME SRC DEST [...]|run NAME|status|panic"
    if not args:
        print(usage, file=sys.stderr)
        return 2
    sub, rest = args[0], args[1:]
    try:
        store = router.PatchStore.load()
        if sub == "list" and not rest:
            for name, patch in sorted(store.patches.items()):
                print(f"{name}  ({len(patch.routes)} route{'s' if len(patch.routes) != 1 else ''})")
            if not store.patches:
                print(f"no patches yet ({store.path}); add one with: fm1-switch route add NAME SRC DEST")
            return 0
        if sub == "show" and len(rest) == 1:
            patch = store.patches.get(rest[0])
            if patch is None:
                print(f"no patch named {rest[0]!r}", file=sys.stderr)
                return 1
            for r in patch.routes:
                print(r.to_dict())
            return 0
        if sub == "delete" and len(rest) == 1:
            store.delete(rest[0])
            print(f"deleted {rest[0]}")
            return 0
        if sub == "add" and len(rest) >= 3:
            name, src, dest, *opts = rest
            kw: dict = {}
            it = iter(opts)
            for flag in it:
                val = next(it, None)
                if val is None:
                    raise router.RouteError(f"{flag} needs a value")
                if flag == "--channels":
                    kw["channels"] = tuple(int(x) for x in val.split(","))
                elif flag == "--remap":
                    kw["remap_to"] = int(val)
                elif flag == "--transpose":
                    kw["transpose"] = int(val)
                elif flag == "--block":
                    kw["block"] = tuple(x for x in val.split(",") if x)
                elif flag == "--velocity":
                    kw["velocity_scale"] = float(val)
                elif flag == "--range":
                    lo, _, hi = val.partition("-")
                    kw["low_note"], kw["high_note"] = int(lo), int(hi)
                else:
                    raise router.RouteError(f"unknown option {flag}")
            old = store.patches.get(name)
            route = router.Route(src, dest, **kw)
            routes = (old.routes if old else ()) + (route,)
            store.save_patch(router.Patch(name, routes))
            print(f"{name}: {src} -> {dest} added ({len(routes)} route(s))")
            return 0
        if sub == "status" and not rest:
            import json as _json
            print(_json.dumps(router_service._read_json(router_service.status_path()) or
                              {"running": False}, indent=2))
            return 0
        if sub in ("run", "panic"):
            from .alsa_seq import AlsaSeq, SeqError
            resolve = router_service.registry_resolver()
            if sub == "panic":
                with AlsaSeq("fm1-router-panic") as seq:
                    n = router_service.panic_all(seq, resolve, print)
                print(f"sent {n} messages")
                return 0
            if len(rest) > 1:
                print("usage: fm1-switch route run [NAME]", file=sys.stderr)
                return 2
            from . import config
            explicit = rest[0] if rest else None
            default = config.setting("FM1_PATCH", "patch", "default")
            if explicit and explicit not in store.patches:
                print(f"no patch named {explicit!r} (patches: {', '.join(store.patches) or 'none'})",
                      file=sys.stderr)
                return 2
            import signal
            import threading
            stop = threading.Event()
            # SIGTERM (systemd stop) and SIGINT (also ignored in a background shell job)
            # both end the loop cleanly, so units are released and the status file removed.
            for sig in (signal.SIGTERM, signal.SIGINT):
                signal.signal(sig, lambda *_: stop.set())
            log = lambda m: print(m, flush=True)  # noqa: E731  (journald timestamps it)
            reload = router_service.PatchFollower(explicit, default, log)
            try:
                with AlsaSeq("fm1-router") as seq:
                    svc = router_service.RouterService(seq, resolve, log=log)
                    log(f"router up; patch {reload.name!r}; stop with SIGTERM or Ctrl-C")
                    svc.run(stop, reload=reload)
            except SeqError as e:
                print(f"cannot reach the ALSA sequencer: {e}", file=sys.stderr)
                return 1
            return 0
    except (router.RouteError, ValueError) as e:
        print(f"route: {e}", file=sys.stderr)
        return 1
    print(usage, file=sys.stderr)
    return 2


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        print(USAGE)
        return 0

    cmd = args[0]
    if cmd in ("-h", "--help", "help"):
        print(USAGE)
        return 0
    if cmd == "list":
        return cmd_list()
    if cmd == "import":
        return cmd_import()
    if cmd == "diagnose":
        return cmd_diagnose()
    if cmd == "web":
        return cmd_web(args[1:])
    if cmd == "hub":
        return cmd_hub(args[1:])
    if cmd == "fetch":
        return cmd_fetch(args[1:])
    if cmd == "inspect":
        return cmd_inspect(args[1:])
    if cmd == "download":
        return cmd_download(args[1:])
    if cmd == "units":
        return cmd_units()
    if cmd == "register":
        return cmd_register(args[1:])
    if cmd == "route":
        return cmd_route(args[1:])
    if cmd == "set":
        return cmd_set(args[1:])
    if cmd == "asset":
        from . import assets_cli
        return assets_cli.main(args[1:])

    print(f"unknown command {cmd!r}\n\n{USAGE}", file=sys.stderr)
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
