# fm1-switch

The Python package behind the FM-1 Fleet Manager: a web server and JSON API, the touch-panel
UI (`/fleet`), the expert UI (`/`), guided flash-by-flash-id orchestration, and a CLI.

See the top-level [README](../README.md) for what it is, its status, install steps and
safety model. [docs/AUDIT-fleet-manager.md](../docs/AUDIT-fleet-manager.md) §2 has the
module map and the full API table.

Facts that earlier versions of this file got wrong:

* It **is** a daemon: `fm1-switch web` runs a threaded HTTP server (normally as a systemd
  user service).
* It **keeps state** in `~/fm1-units.json` (registry), `~/fm1-flash.log` (audit log),
  `~/fm1-backups/` (whole-flash backups) and `~/fm1-hub/` (catalogue and inspection caches).
* It **uses the network.** It reads The Firmware Hub's catalogue (at most once a day) and
  downloads a package only when asked, then verifies its SHA-256 and size.
* It runs on Python's standard library only, with no third-party runtime dependencies.

```bash
fm1-switch web [--host H] [--port N]   # the server (127.0.0.1 by default — the panel's
                                       # path; --host 0.0.0.0 + FM1_WEB_HOSTS=<name>
                                       # opts in to the LAN, and off-Pi writes then
                                       # need the printed X-FM1-Token)
fm1-switch units                       # what the Pi thinks each unit is, and how sure it is
fm1-switch diagnose                    # m-flasher, sg driver, udev symlinks
fm1-switch hub [query] | fetch <id> | inspect <file.fwsc>
```

**`fm1-switch install` was removed** (audit SAFE-01): it wrote by USB port, without a
flash-id match, backup, lock or log. The panel — and `/api/flash`, which runs the same
id-matched `guided.flash` — is the only writer.

Tests: `python -m pytest -q` and `node --test tests/js/fleet.test.mjs` (see `../CONTRIBUTING.md`).
