# Audit — FM-1 Fleet Manager, and readiness for open-source release

**Date:** 2026-11-23 · **Commit audited:** `94a0442` (branch `main`, 28 commits, no remote)
**Scope:** `fm1-switch/` (the product), `fleetctl/`, the root bench scripts, `docs/`, systemd units, udev rules.
**Method:** read every source file. Ran the three test suites. Rendered the real panel files at
800×480 in a Chromium-based browser against a stub server that returns canned JSON and refuses
every POST. Ran the real server bound to `127.0.0.1` against temporary directories, sending GETs
and POSTs that were expected to be refused. Swept git history. No hardware, no network,
no `m-flasher` (confirmed absent: `which m-flasher` → not found).

> `docs/HANDOFF-fleet-manager.md` and `docs/FM1-pi-arrival-runbook.md`, cited below, were moved
> to the gitignored `docs/private/` after this audit (they hold bench-private details).
>
> This file is meant to be publishable. It deliberately does **not** repeat LAN addresses,
> hostnames, usernames, SSH key paths, e-mail addresses or unit flash ids. Where those exist in
> the repo, this report cites the file and line instead.

**Epistemic labels used throughout**

| Label | Meaning |
|---|---|
| **[measured]** | I ran it on this Mac and quote the output |
| **[derived]** | I read the code and reasoned through it. Not executed |
| **[documented]** | A repo doc or commit message claims it. Not independently verified |
| **[assumed]** | Neither. Stated so it can be checked |

**Severity:** **P0** blocks release · **P1** fix before sharing · **P2** schedule · **P3** nice-to-have.

---

## 1. Executive summary

**What it is.** A Raspberry Pi 3B+ with a 5" 800×480 resistive panel runs a stdlib-only Python web
server (`fm1-switch`). It shows a kiosk page (`/fleet`) in `cog`. Its one job is to put the
right firmware on the right one of four M-VAVE FM-1 synths. USB cannot tell running units apart,
so the server identifies a unit by its flash-chip id, which can only be read in download mode.
`guided.flash` puts units into download mode with a MIDI soft key, reads every candidate's
id, backs up the match and writes only that one.

**Verdict.** The core write path (`guided._flash_locked`) is careful, honest and well tested
against a fake bus. It is the best part of the codebase. Around it, the product has gaps:

* the panel's confirmation can be completed by one held press in one measured case;
* there is a second, unguarded write path (the CLI `install`);
* the API accepts cross-origin requests;
* since the console UI was deleted, nothing can bind a flash id to a sticker. A new bench can
  never reach a flashable state from the panel.

For open-source release the repo also carries the owner's unit flash dumps and bench-private
details, in every commit since the first, and it has no licence. **Not ready to publish as is.**
The P0 list is short and concrete, but it involves code, so Phase B owns it, not this audit.

### Grades

| Dimension | Grade | One line |
|---|---|---|
| Architecture | **B−** | Clear single-purpose modules with unusually good "why" docstrings. But `web.py` mixes four jobs, identity is read two incompatible ways (udev port symlinks vs sysfs), and two overlapping tools ship. |
| Correctness / safety | **C+** | `guided.flash` holds every invariant it owns. The gaps are around it: CLI bypass, optional sha check, unlocked flash reads during writes, unverified backups, one measured press-through. |
| Tests | **B−** | 170 + 34 + 47 tests, real m-flasher output pinned, real packages pinned. Nothing tests concurrency, layout, the HTTP layer end to end with a fake device, or CSRF. |
| Features | **B** | Rich for its size. Registration (id → sticker) is missing entirely, and the six §13.3 panel features have no hardware proof in the repo. |
| UX (panel) | **C+** | Strong visual language and honest copy. One measured press-through, sub-96 px targets everywhere except the modal, an empty-fleet dead end, invisible unmatched units, and one explanation that can be wrong. |
| Documentation | **C** | Deep and candid, but nine docs disagree with each other and with the code, two READMEs are wrong, bench-private details are throughout, and there is no top-level README. |
| Security | **D+** | No authentication, binds `0.0.0.0`, and a **measured** CSRF-shaped hole (text/plain JSON, no Origin/Host check). Unit ids are public in the repo. Static serving leaks `web.py` source. |
| OSS readiness | **D** | No LICENSE (and `fleetctl` declares MIT without one). Owner's dumps and third-party binaries tracked in all history. Hardcoded user and paths. A stranger cannot register a unit. |

### Top 10 findings by impact

| # | ID | Sev | Finding |
|---|---|---|---|
| 1 | UX-01 | **P0** | On the SEVERAL screen, `FLASH n UNITS` sits under the modal's `WRITE`: **13,366 px² overlap [measured]**. One stationary press can arm and confirm a multi-unit write, which breaks invariant 2. `BACK UP` overlaps its own confirm too (6,630 px²). |
| 2 | SEC-01 | **P0** | A cross-origin `text/plain` POST is parsed as JSON. There is no Origin, Host or token check **[measured]**. Any web page the owner opens can drive `/api/flash`, `/api/restore`, `/api/backup/delete`, `/api/library/remove`. The ids it needs are committed in the repo. |
| 3 | SAFE-01 | **P0** | `fm1-switch install SLOT PKG` writes through the port-bound `/dev/fm1-x` with no flash-id match, no backup, no lock, no log and no sha check (`__main__.py:61-81`). The package README tells users to run it (`fm1-switch/README.md:63`). |
| 4 | FEAT-01 | **P0** (OSS) | Nothing can bind a flash id to a sticker. The only caller of `Registry.bind` was `console_ui.py`, deleted in `814c556` **[measured via git]**. `registration.confirm` has no caller. A new bench never gets a flashable unit, and the panel's empty state sends the user to a SETTINGS screen that cannot help. |
| 5 | SAFE-02 | P1 | The sha256 re-check is skipped when the client omits `sha256` (`web.py:188`). Measured: an omitted sha passed `_check_package` and was refused only later, by the missing hardware. The audit log then records the server's own hash as if the user had confirmed it. |
| 6 | SAFE-03 | P1 | Flash reads can run during a write without the lock: `/api/units?probe=1` (the expert page's Refresh and pre-write probe), `/api/fleet` whenever `last_fleet` is `None` (e.g. right after a rename), and all of `restart_unit` (which never sets `busy`). Each runs `m-flasher info` on a download-mode device, possibly the one being written. |
| 7 | SAFE-04 | P1 | The pre-write backup is trusted on exit code alone (`web.py:388-396`). Nothing checks that a `.bin` and `.json` naming this unit with a matching sha actually landed, so "backup before every write" rests on m-flasher's exit code. |
| 8 | SAFE-05 | P1 | The audit log is incomplete and in places misleading. Refusals raised before `guided` (confirm mismatch, sha drift, bad name, bad backup path) are never logged **[measured]**. A write that failed mid-run is logged as `refused`. A failure to write the log is silently swallowed. |
| 9 | UX-03 | P1 | After a write, `attention()` can report "DIDN'T RESTART: POWER-CYCLE IT, OR RESTORE…" for a unit that did restart. It fires whenever relearn returns `not-running` (a 25 s timeout) or `not-saved`. Running units that no binding matches are dropped from `/api/fleet` altogether. Restore overwrites presets, so this is the explanation most likely to cause harm. |
| 10 | ARCH-02 | P1 | `/api/fleet` finds download-mode units only through the bench-specific udev symlinks (`registry.scan` → `units.enumerate_units`), while `guided` uses sysfs. On any other bench, or on an unmapped port, no unit ever shows READY or an id. The udev file's comment claims hub-model pinning that the rules do not implement. |

Also notable: `fleetctl`'s m-flasher wrapper uses `stdin=DEVNULL`, so it has the exact
"Nothing written" bug the main package fixed (§3.10). Static serving leaks `fm1_switch/web.py`
through a prefix check (§9). A new-chip compatibility lookup can reach the network on a page
render (§3.6).

---

## 2. System map (as verified)

### 2.1 Architecture

```
                     LAN (bench Wi-Fi)                        Pi 3B+ (Debian trixie, aarch64)
 phone / laptop ─────── HTTP :8080 (0.0.0.0, no auth) ───┐
                                                         ▼
 5" panel ── cog (WPE, DRM, system unit) ── 127.0.0.1:8080 ─► fm1-switch web (user unit)
                                                         │   ThreadingHTTPServer, web.Handler
                                                         │   ├─ /fleet  fleet.html/js/css  (panel)
                                                         │   ├─ /       index.html/app.js  (expert)
                                                         │   └─ /api/*  JSON (§2.2)
                                                         │
             ┌───────────────────────────────────────────┼──────────────────────────────┐
             │ guided.flash (flash/restore/backup)       │ registry.known (fleet poll)  │
             │   download_devices(): /sys/class/scsi_generic   scan(): /dev/fm1-* (udev, │
             │   softkey.soft_key(port): SysEx to /dev/snd/midiCnD0     PORT-BOUND)      │
             │   mflasher.info/backup/firmware/restore/restart          running(): sysfs │
             └───────────────┬───────────────────────────┴──────────────┬───────────────┘
                             ▼                                          ▼
                  m-flasher (subprocess, argv, input="yes\n")   ~/fm1-units.json (registry)
                             ▼                                  ~/fm1-flash.log  (audit)
               FM-1 ×4 over USB-C hubs                          ~/fm1-backups/{auto,manual}
               running: 1209:0001 / 4d4a:4155                    ~/fm1-firmwares  (library)
               download: 4c4a:8057 (SCSI generic)                ~/fm1-hub/{catalog,inspect}.json
                                                                 thefirmwarehub.com (catalogue, packages)
```

The correction to the mental model in `docs/` **[derived]**: identity has two discovery
paths that disagree. Writes find download-mode devices structurally, through sysfs
(`guided.py:74-83`). The fleet view finds them only through udev symlinks bound to hardcoded
ports (`registry.py:354`, `units.py:47-60`). The module docstrings of `web.py` (rules 1–2,
`web.py:11-16`) and `softkey.py` (`:47-48`) still describe the old port-addressed design.

### 2.2 API inventory (from `web.py`)

All responses are JSON with `Cache-Control: no-store`. Errors are `{"error": str}`. POST bodies are
parsed as JSON **whatever their Content-Type** (`web.py:537-547`; length ≤ 1 MB, otherwise `{}`).
`WriteRefused` → 409 and `HubError` → 502. Any other exception escapes to `socketserver`, which
drops the connection and writes a traceback to stderr.

| Method | Path | Request | Response | Side effects | Lock? | Error modes |
|---|---|---|---|---|---|---|
| GET | `/fleet`, `/fleet/` | – | fleet.html | – | – | – |
| GET | `/`, `/<file>` | path | static file | – | – | 404; **prefix-check leak, §9 SEC-03** |
| GET | `/api/viewport` | `vw,vh,dpr,screen` | `{ok,logged}` | **appends to `/tmp/fm1-viewport.log`, unbounded** | – | OSError ignored |
| GET | `/api/fleet` | – | `{units[], labels, build, busy, last_event, now}` | runs `registry.known()` → `m-flasher info` per udev-mapped download-mode unit; may hit network via nothing (catalogue cache only) | **no** (serves cache only if `busy` **and** `last_fleet`) | – |
| GET | `/api/fleet/package` | `name, chip` | `{name, sha256, identity, severity, headline, details, compat}` | hashes file; parses package; **`compat_for` → `hub.load()` with network enabled** for new-chip units | no | 400/404 |
| GET | `/api/state` | – | `{units[] (port slots), busy, last_event, catalog, attribution}` | – | no | – |
| GET | `/api/units` | `probe=0|1` | as `/api/state` | `probe=1`: **`m-flasher info` on every `/dev/fm1-*`** | **no** | per-unit `error` |
| GET | `/api/catalog` | `q, kind, chip` | `{entries[], total, meta, attribution, kinds}` | may fetch catalogue (≤1/day unless forced) | no | `{error, entries:[]}` with 200 |
| GET | `/api/library` | – | `{items[], library_dir}` | sha256 of every package (uncached, `library._digest`), inspect | no | – |
| GET | `/api/package` | `name` | package detail | – | no | 400/404 |
| GET | `/api/verify` | – | `{packages[]}` | rehash all; may fetch catalogue | no | – |
| GET | `/api/backups` | `unit_id` | `{items[], space}` | walks backups dir | no | `[]` for non-hex id |
| GET | `/api/getlist` | – | `{items[]}` | catalogue cache only | no | `[]` |
| GET | `/api/flashlog` | – | `{items[]}` (8 newest done/refused/restart) | – | no | `[]` |
| POST | `/api/flash` | `{unit_id, package, sha256?, confirm}` | `{ok, event}` | guided write: soft key, restarts, **backup**, write, relearn registry, log | yes (non-blocking) | 409 |
| POST | `/api/restore` | `{unit_id, backup, confirm}` | `{ok, event}` | same guided path with `restore_image(keep_bootloader)` | yes | 409 |
| POST | `/api/backup` | `{unit_id, confirm}` | `{ok, event}` | guided path with a copy-only writer into `manual/` | yes | 409 |
| POST | `/api/restart` | `{unit_id, confirm}` | `{ok, message}` | `m-flasher restart` on the id-matched device; log | yes, **but `busy` not set** | 409 |
| POST | `/api/backup/delete` | `{unit_id, backup, confirm}` | `{ok, removed}` | **unlinks .bin + .json**; log | **no** | 409 |
| POST | `/api/rename` | `{unit_id, name, confirm}` | `{ok, name}` | rewrites registry; log; **sets `last_fleet=None`** | **no** | 409 |
| POST | `/api/download` | `{id}` | `{ok, path, name, sha256}` | network fetch into library (sha+size verified) | no | 502 |
| POST | `/api/catalog/refresh` | – | catalogue | **forced network fetch, ignores the once-a-day rule** | no | 502 |
| POST | `/api/library/remove` | `{name}` | `{ok, removed}` | **unlinks a library package; no confirm; not logged** | no | 409 |

### 2.3 Module map and verdicts

| File | Lines | Purpose | Verdict |
|---|---|---|---|
| `fm1_switch/web.py` | 880 | HTTP, routing, static, app state, write wrappers, backups, updates, caches | **Refactor**: split into `http.py` (handler/routing/static), `ops.py` (flash/restore/backup/delete/rename + log), `views.py` (fleet/update/getlist). Rewrite the stale module docstring. |
| `guided.py` | 279 | id-matched write orchestration | **Keep.** Fix docstring (broadcast claim), §4. |
| `mflasher.py` | 297 | the only subprocess site | **Keep**; add "device required for writes", remove duplicate `--device` in `download_mode` (§3.3). |
| `registry.py` | 531 | bindings, live view, fingerprint pairing | **Keep**; make `scan()` use sysfs like `guided` (ARCH-02); add a lock around load/modify/save. |
| `registration.py` | 219 | power-on attribution | **Keep**; wire `confirm()` to an API (FEAT-01). |
| `softkey.py` | 220 | SysEx to a port's MIDI node | **Keep**; docstring says "broadcast" and "writes go to `/dev/fm1-<letter>`" — both outdated. |
| `packages.py` | 539 | `.fwsc` parser, verdict, disk cache | **Keep**; cache not thread-safe (§3.6). |
| `hub.py` | 455 | catalogue client, package fetch | **Keep**; rate-limit forced refresh; fix "we show author + repo" claim (`hub.py:13`). |
| `library.py` | 121 | library scan/import/remove | **Keep**; docstring "no network code" is true of the module but misleading about the system. |
| `compat.py` | 43 | chip compatibility | **Keep**; must use `network=False` (§3.6). |
| `units.py` | 105 | slots, udev symlink enumeration, port map | **Refactor then shrink**: `SLOTS`/`SLOT_LABELS` are still the 4-unit identity namespace; `enumerate_units`/`read_port_map` belong to the port era. `read_port_map` is duplicated verbatim in `web.py:78-91`. `candidates()`, `list_download_devices()` have no callers **[measured: grep]**. |
| `touch.py` | 185 | evdev tap reader | **Move** to `tools/` (only `tools/calibrate.py` imports it) or delete with the retired console. |
| `__main__.py` | 419 | CLI | **Refactor**: delete or guard `install` (SAFE-01); `register` tells the user to "run `fm1-switch units`" to confirm, which does not confirm anything (`:349`). |
| `__init__.py` | 8 | version | Fix docstring: "daemon-less" is false; "m-flasher already enforces … read-back verify" is **[documented]** by nobody here. |
| `web/fleet.*` | 51+256+853 | panel UI | **Keep**; findings §7. |
| `web/index.html`, `app.js`, `style.css` | 74+487+234 | expert UI | **Refactor** to registry/flash-id terms or reduce to catalogue + library (§7.6). |
| `tools/make-edid.py` | 112 | EDID builder/validator | **Keep** (panel-specific; document as such). |
| `tools/calibrate.py`, `calibrate-touch.sh`, `check-touch.sh` | – | touch bring-up | Keep under `tools/bench/`; `calibrate.py:20` hardcodes a home path. |
| `tools/dump-console.sh` | 18 | read `/dev/vcs1` | **Retire** with the console UI. |
| `tools/settle-saves.sh` | 169 | save-survival experiment | Keep as a documented experiment. |
| `tools/fetch-realtest.sh` | 77 | fetch test packages | **Keep and fix** (§5.4). |
| `systemd/fm1-web.service`, `fm1-kiosk.service` | – | services | **Template** (hardcoded user/home); kiosk `Documentation=` points at a retired doc. |
| `systemd/fm1-screen.service`, `console-setup` | – | retired console | **Delete** (keep in history). `fm1-kiosk.service:11-12` still `Conflicts=`/`After=` it. |
| `fleetctl/` (whole package) | ~870 + tests | earlier port-addressed CLI | **Retire** (§3.10); salvage nothing but the udev rule's *knowledge*. |

**Dependency graph [measured: imports]:** `web → guided, hub, library, mflasher, packages, registry, units, compat`;
`guided → mflasher, registry, softkey`; `registry → mflasher, units`; `registration → registry, softkey, units`;
`compat → hub, library`; `__main__ → library, mflasher, softkey, units (+ lazy web, hub, packages, registry, registration)`.
There are no cycles. `units` is a leaf that everything depends on for `SLOTS`.

---

## 3. Code review

### 3.1 Cross-cutting

**[P2] ARCH-01 — `web.py` has four jobs and a stale contract**
Evidence: `web.py:1-24` (docstring says writes are addressed by "slot" and "udev symlink", and that "reads of unit state take the same lock briefly", which they do not, §4 I-7); `:94-160` app state; `:179-500` operations; `:503-851` HTTP.
Why: a reviewer reads the docstring as the contract. It promises a lock that the code does not take.
Fix: split as in §2.3. Replace the docstring with the Appendix A invariants and pointers to each
enforcement site. Acceptance: every module docstring states only what its code does (a reviewer
checklist item); `web.py` ≤ 300 lines.
Risks: pure move. Run the suites before and after; no behaviour change permitted in the same commit.

**[P2] ARCH-03 — the stdlib-only, no-build constraint: keep it**
Evidence: `pyproject.toml` `dependencies = []`; no `package.json`; JS tests use a hand-written fake DOM (`tests/js/fleet.test.mjs:16-35`).
What it buys: deploy by copying files, no supply chain on a box that writes firmware, a cold
start that fits in 1 GB, and readable code. What it costs: (a) an HTTP layer with no CSRF/Origin
handling and no request framework, so security is entirely hand-rolled (§9); (b) a fake DOM that
cannot catch layout, CSS or async-render bugs (§5.3, measured); (c) threading by hand with
shared module globals (§3.2).
Recommendation: **keep the runtime constraint**. The costs are paid in test and review, not at
runtime. Add dev-only test tooling (Playwright run from CI, never installed on the Pi) and
small stdlib helpers: an `@guarded_post` decorator that checks Origin and token, and a single
`state_lock`. Acceptance: Pi runtime still `dependencies = []`; dev tooling lives under
`[project.optional-dependencies] dev` or CI only.

### 3.2 Threading and shared mutable state

`ThreadingHTTPServer` runs every request on its own thread. Shared state **[derived]**:

| State | Where | Writers | Readers | Guard | Race |
|---|---|---|---|---|---|
| `APP.lock` | `web.py:98` | guided.flash, restart_unit | `_run_guided` finally | itself | – |
| `APP.busy` | `:103` | `phase()` inside guided (lock held); `save_and_restart` (`:363`); `_run_guided` finally (if lock free) | `/api/fleet`, `/api/state` | none | `restart_unit` never sets it (`web.py:820`), so a poll during a restart scans (R1) |
| `APP.last_event` | `:104` | `_run_guided` on refusal **including "another flash is already running"** (`:250`) and success | `/api/fleet` | none | A second client's refused request overwrites the running write's context. The panel may then say "LAST WRITE FAILED: another flash is already running" for that unit (R2) |
| `APP.last_fleet` | `:105` | `_api_fleet` (`:665`), `_api_rename` sets `None` (`:813`) | `_api_fleet` busy branch (`:640`) | none | A rename (or a fresh process) during a write empties the cache, so the next poll runs a full scan → `m-flasher info` on the device being written (R3) |
| `APP.units` | `:102` | `refresh_units` | `/api/state` | none | `/api/units?probe=1` runs `m-flasher info` with no lock (`:137`) (R4) |
| `APP.catalog*` | `:99-101` | load_catalog from any GET/POST | many | none | benign (atomic attribute swap) |
| `_SHA_CACHE`, `_PKG_CACHE` | `:475-476` | any GET | any GET | none | `_PKG_CACHE.clear()` while another thread inserts: benign in CPython |
| `packages._CACHE.data` | `packages.py:475` | `put()` rebuilds dict and writes `inspect.tmp` | `get()` | none | two concurrent `put()`s write the same `.tmp` path and race `replace()`; a dict comprehension over `self.data` while another thread reassigns it can raise (R5) |
| `~/fm1-units.json` | `registry.save` | `rename` (HTTP thread), `relearn_after_flash` (inside the write) | every poll | atomic replace, no lock | load-modify-save interleaving loses one update: a rename during a write's relearn can drop the new fingerprint, and the unit then shows NOT CONNECTED (R6) |
| `~/fm1-flash.log` | `_log_flash` | many threads | flashlog | `O_APPEND` per line | OK on Linux for lines < PIPE_BUF; acceptable |
| library files | `/api/download`, `/api/library/remove` | any | flash | none | removing a package between `_check_package` and the m-flasher call makes the write fail; it fails safe (R7) |

**Multi-flash loop vs. server lock [derived]:** the loop lives in the client (`fleet.js:604-625`).
Between two units the lock is free. A second client can win that gap and its request
succeeds, and the loop's next POST is then refused with "another flash is already running".
The loop stops and reports it. That fails safe. Restore vs flash share the lock.
Build-change reload is suppressed while `writing()` (`fleet.js:83`).

**[P1] CONC-01 — one state lock, and the busy flag set for every device-touching operation**
Fix: (1) Introduce `APP.state_lock` (RLock) around `last_event`, `last_fleet`, `busy`.
(2) Make every `m-flasher info` call site acquire `APP.lock` non-blocking, and serve cached or
"busy" data when it is held: `refresh_units`, `registry.scan` via `_api_fleet`, `restart_unit`.
(3) Have `restart_unit` call `on_phase("Restarting…")`. (4) Do not write `last_event` for the
"already running" refusal. (5) Wrap registry load-modify-save in a module-level
`threading.Lock` (`registry._io_lock`).
Acceptance: a new test starts a fake write that blocks inside `writer`, then fires
`/api/units?probe=1`, `/api/fleet` (with `last_fleet=None`), `/api/rename` and `/api/restart`
concurrently. It asserts that the fake `mflasher.info` is never called while the writer runs,
and that the registry ends with both the rename and the relearned fingerprint.
Risks: touches invariant 7. A blocking acquire anywhere would queue requests, which the
invariant forbids, so every acquire must stay non-blocking.

### 3.3 `mflasher.py`

* **[P2] MF-01 — device is optional for every command.** `run()` omits `--device` when `device`
  is falsy (`mflasher.py:103-105`). No write path passes `None` today **[derived]**, but nothing
  enforces it. Fix: `if args[0] in WRITING and not device: raise MflasherError(...)`, where
  `WRITING = {"firmware","restore","bootloader","backup","restart","download-mode"}`. Acceptance:
  a unit test calls each writer with `device=None` and expects an exception.
* **[P3] MF-02 — `download_mode(device)` passes `--device` twice** (`argv_download_mode` adds it,
  `run` adds it again: `:248-249`, `:296-297`). It has no callers **[measured: grep]**. Delete it, or fix it.
* **[P1] MF-03 — `_check_written` reads stdout only, by regex** (`:140-156`). If m-flasher
  prints "Nothing written" to stderr, or rewords it, a non-write passes as success. Fix: check
  `stdout + stderr`, and for `firmware`/`restore`, also require a positive marker that the tests
  already pin (`test_successful_write_still_succeeds` uses "Done: everything written is
  verified"). Treat neither marker present as `NothingWritten`. Acceptance: tests for stderr-only
  and unmarked output. Risks: a positive-marker rule can make a correct write report failure
  if m-flasher's wording changes. That is the right direction to fail (invariant 9), but it
  needs the verbatim 0.12.1 and 1.3 outputs captured from hardware first (§5.2).
* `input="yes\n"` is set in exactly one place (`:117`). All writes route through `run()` **[derived:
  every `subprocess` call in `fm1_switch` is this one, `grep subprocess` → web.py imports but never calls it]**.
  `web.py:34` imports `subprocess` unused: delete it.

### 3.4 `guided.py`

Strong. Each step re-reads ids rather than assuming them, and every exit path says what was
and was not written. Findings:

* **[P2] GD-01 — docstring calls the soft key a broadcast** (`guided.py:17-20`, `:96-98`), while the
  handoff records it as pointed (`HANDOFF-fleet-manager.md:468`, `:714`, "D alone moved"). The code
  handles either case correctly. Fix the prose to say "pointed in every measured case since
  2026-11-22; the code does not rely on that".
* **[P1] GD-02 — a failed write is reported as a refusal and leaves the unit in download mode
  silently.** `:196-199` converts `MflasherError` from the writer into `WriteRefused`.
  `_run_guided` logs `event="refused"` (`web.py:252`), and the panel says "FAILED: …". A
  partially written unit and a refused request look the same in the log. Fix: raise a
  distinct `WriteFailed(WriteRefused)` carrying `stage="write"`, log `event="failed"`, and add
  "the unit may be partly written; restore from the backup taken at <time>" with the auto-backup id.
  Acceptance: test with a writer raising `TimedOut`; the log shows `failed` with the backup id, and
  `/api/fleet` `last_event.stage == "write"`.
* **[P2] GD-03 — relearn timeout equals the download-mode timeout (25 s)** (`:113`, `:204`, `:236-247`).
  The handoff measures writes at ~32 s, and boot time after `--restart` is not measured. A slow
  boot produces `not-running` and the false "DIDN'T RESTART" (UX-03). Fix: separate
  `boot_timeout` (60 s) and return `"late"` rather than `"not-running"` when the port later shows a
  running id. Acceptance: fake clock test where the unit returns at 40 s → `relearned == "ok"`.
* **[P2] GD-04 — `before_write` failures other than `MflasherError` escape.** `_backup_before_write`
  can raise `OSError` from `mkdir` (`web.py:391`). That escapes `guided` and `_run_guided` (which
  catches only `WriteRefused`/`MflasherError`, `web.py:249`), drops the HTTP connection, leaves
  the unit in download mode, and leaves a `request` log line with no outcome. Nothing is
  written, so it fails safe, but silently. Fix: catch `OSError` alongside `MflasherError` in `:185-193`.

### 3.5 `registry.py` / `registration.py`

* **[P0 for OSS, P1 for the bench] FEAT-01** — see §6.1.
* **[P1] ARCH-02 — `scan()` sees only udev-mapped ports.** `registry.py:354` iterates
  `units.enumerate_units()` (`/dev/fm1-a..d`). A unit in download mode on any other port has no
  row: not READY, no id, no RESTART UNIT. Meanwhile `guided.download_devices()` finds it through
  sysfs. Fix: replace the loop with `guided.download_devices()` (move that function into
  `registry`, since `guided` imports `registry` and not the other way round). Acceptance: a test
  with a fake sysfs device on an unmapped port; `known()` returns it with its id and
  `in_download_mode`. Risks: more devices get `m-flasher info` per poll. Serialise them under the
  lock (CONC-01).
* **[P2] REG-01 — unmatched running units vanish.** `known()` adds only paired runs
  (`registry.py:401-405`). Contrast "whatever still cannot be paired is left unlabelled" (`:436`):
  it is in fact left out. The fleet summary "N RUNNING" (`fleet.js:121-122`) undercounts.
  Invariant 1 says "unlabelled units show `?`". Fix: append unmatched runs as `LiveUnit(unit_id=f"port:{port}", running=True, slot=None)`.
  They then render `?` with NOT REGISTERED. Acceptance: test with 2 runs sharing a fingerprint
  and no port tie-break → 2 `?` rows.
* **[P2] REG-02 — vendor filter catches non-FM-1 devices.** `FM1_VENDORS = {"1209","4d4a","4c4a"}`
  (`:493`). `1209` is pid.codes, a shared VID for open-hardware projects, and `1209:0001` is its
  test PID. On a stranger's bench, any pid.codes gadget is listed as a running FM-1. If it has
  a MIDI node it becomes a soft-key target (`guided._soft_key_ports`, `:103-105`), receiving
  `F0 22 24 35 7D F7`. Fix: match `vendor:product` pairs plus the product string
  (`"Felucca"`, `"M-VAVE FM-1"`, `WL8x UBOOT`), configurable. Acceptance: fake sysfs with
  `1209:5bf0` → not listed.
* `_match_running` worst case **[derived]**: it pairs only when a fingerprint is unique on both
  sides, or a shared one is broken by the last-seen port. Swapping the cables of two units on
  the same firmware between setups swaps their labels on the fleet view, because both reported
  ports changed. **Writes are unaffected** (they match by id after a soft key). The soft key is
  sent to the *labelled* unit's port first (`guided.py:100-102`), so the wrong unit may enter
  download mode, then `guided` reads its id, finds no match, restarts it and writes nothing.
  The cost is a misleading card and a refused write. Fix: mark a port-tie-broken label as
  provisional in the API (`"hint": "port"`) and render it with `~` (UX-05).

### 3.6 `packages.py`, `hub.py`, `compat.py`, `library.py`

* **[P2] HUB-01 — a page render can reach the network.** `compat_for` calls `hub.load()` with the
  default `network=True` (`compat.py:30`). When the cache is stale it fetches the catalogue
  inside `GET /api/fleet/package`, which the panel issues per package for a new-chip unit
  (`fleet.js:263-274`). The timeout is 60 s (`hub.py:297`). Offline with a stale cache, each
  request retries. The handoff says panel paths use the "cached catalogue only"
  (`HANDOFF:742-743`), which is not true here. Fix: `hub.load(network=False)`, or use
  `web._catalog_cached()`. Acceptance: a test monkeypatches `urllib.request.urlopen` to raise
  and asserts it is never called from `/api/fleet/package`.
* **[P2] HUB-02 — forced refresh bypasses the hub's once-a-day term.** `POST /api/catalog/refresh`
  sets `force=True` (`web.py:618-620`, `hub.py:314`). Any client can loop it. Fix: honour `force`
  only if the cache is older than 1 h, and log it. Acceptance: two refreshes in a row → one fetch.
* **[P1 for OSS] HUB-03 — maker credit and repository links are not shown.** The hub's terms
  (`hub.py:10-15`) require naming each firmware's author and linking its repository. The expert
  cards show `by <author>` (`app.js:347`) and **no repository link** (`grep repo app.js` → none).
  The panel's GET FIRMWARE list shows neither author nor attribution (`fleet.js:790-796`).
  `hub.py:13` claims "We show author + repo on every card". Fix: expert card adds
  `<a href=repo>`; panel GET FIRMWARE rows add "by <author>"; the GET FIRMWARE footer shows
  `hub.ATTRIBUTION` (short form). Acceptance: JS test asserts author text in each getlist row.
* **[P2] PKG-01 — the inspect cache is not thread-safe** (R5 above). Fix: a module lock around
  `get`/`put`, and a unique temp name (`tempfile.NamedTemporaryFile(dir=..., delete=False)`).
* **[P2] PKG-02 — the verdict is a heuristic presented as a fact.** "Installs completely from
  m-flasher." (`packages.py:534`) is derived from a 3-name threshold fitted on 7 packages
  (`:418-424`). The docstring is honest about this. The UI string is not. Fix: headline "No
  factory sound bank found (checked against 7 known packages)" and keep the evidence in details.
* `library._digest` is uncached and `/api/library` hashes every package on every call
  (`web.py:736`, `library.py:67`). On the Pi that cost is paid on every panel `loadFleet()`
  (`fleet.js:87`, every 5 s on the fleet view). Use `web._sha256`'s cache. **[P2]**

### 3.7 `__main__.py`

* **[P0] SAFE-01 — `install` bypasses the safety model.** `cmd_install` (`:61-81`) resolves
  `/dev/<slot>` (a port symlink), then calls `mflasher.install_firmware` directly. It skips the
  flash-id match, backup, lock (so it can run concurrently with the web server's write), audit
  log, sha check, and any confirmation. `fm1-switch/README.md:63` documents it as the way to
  flash. Fix: delete `install`, or reimplement it as `fm1-switch flash <unit-id> <package>`
  that calls `web.flash_unit_by_id` (through a small shared `ops` module) with an interactive
  "type the last 4 of the id" confirmation and the same log. Across processes use an `flock` on
  `~/.fm1.lock`, because the web server's `threading.Lock` does not cover another process.
  Acceptance: `fm1-switch install` no longer exists, or it refuses without an id, and a test
  asserts the CLI calls the guarded path. Risks: touches invariants 2, 4, 6, 7, 10. This finding
  is about restoring them, not trading them.
* **[P2]** `cmd_register` ends with "run `fm1-switch units`" to confirm (`:349`). That command
  only prints. See FEAT-01.
* **[P2]** `cmd_web` defaults `host="0.0.0.0"` (`:103`). See SEC-02.

### 3.8 Web UIs

See §7 for UX. Code-level:

* **[P2] JS-01 — async renders interleave and duplicate content [measured].** `render()` clears
  `#screen` synchronously, then awaits network or compat calls before appending
  (`renderUnit` `:303`, `renderRestore` `:416`, `renderSettings` `:535`, `renderGet` `:783`). Two
  renders in flight both append. On the RESTORE screen's second page (2 backups), one tap on
  `›` followed immediately by a poll-driven `render()` produced **4 rows instead of 2**
  (stub, 800×480). Fix: a render generation counter (`const gen = ++state.gen;` … after every
  await `if (gen !== state.gen) return;`) or render into a detached fragment and swap at the end.
  Acceptance: JS test that triggers two renders before the fetch resolves and counts `.pair` rows.
* `fleet.js:17` state comment omits `"type"`; `fleet.css:253` uses an undefined `--fg`. **[P3]**
* `fleet.js:129` idle footer on SETTINGS says "FIRMWARE IS MANAGED ON THE WEB ADMIN". The same
  screen offers GET FIRMWARE. **[P3]**

### 3.9 Shell scripts, tools, systemd

| File | Findings | Stranger must change |
|---|---|---|
| `setup.sh` | Sound structure (`set -euo pipefail`, idempotent, SUDO_USER handling). Installs **both** fleetctl and fm1-switch. Says "Bookworm" while the Pi runs trixie (HANDOFF). | Drop fleetctl; template systemd units; generate udev from a config. |
| `label-units.sh` | Port-map era: writes `~/fm1-portmap.txt` and udev rules. Its premise ("the ONLY thing distinguishing them is the physical port path", `:12-14`) is superseded by flash ids. Still needed only because `scan()` uses udev (ARCH-02). | Retire after ARCH-02. |
| `verify-units.sh`, `prepare-archives.sh`, `bundle-archives.sh` | Good verification discipline (UNPINNED vs VERIFIED). They operate on the owner's `archives/`. `prepare-archives.sh` downloads a third party's factory dump (`REPO_RAW=…/Quixotic7/FM1Backups`, `:24`). | Move to a private bench repo or `tools/bench/`, documented as owner-specific. |
| `fleetctl/install.sh` | Installs the bench-specific udev rule globally. | Retire with fleetctl. |
| `tools/fetch-realtest.sh` | Header says "five" packages, but tests need seven (`:2`). References `archives/FM1-firmware-install-model.md`, which is really in `docs/` (`:9`). Writes `.catalog.json` into `realtest/`. Uses `sha256sum` (present on this Mac at `/sbin/sha256sum` **[measured]**; older macOS lack it). It also fetches the catalogue, which is fine under the hub terms. | Fix text; `shasum -a 256` fallback; gitignore `realtest/`. |
| `systemd/fm1-web.service` | Hardcoded user home in `WorkingDirectory`, `FM1_FLASHER`, `FM1_LIBRARY`, `ExecStart` (`:7-10`). User unit. No `UMask=` (files 0644, §9). | Template via `%h` (user unit) and an `EnvironmentFile=%h/.config/fm1/env`. |
| `systemd/fm1-kiosk.service` | Hardcoded `User=`/`Group=`/`WorkingDirectory=` (`:16`, `:34-35`); `Documentation=` → retired doc (`:3`); `Conflicts=`/`After=fm1-screen.service` (`:11-12`). The `ExecStartPre` loop uses `curl` (not installed by `setup.sh`'s package list **[derived]**: `setup.sh` installs usbutils, sg3-utils, evtest, python3-venv/pip). | Template with a drop-in for the user; add `curl` to setup. |
| `systemd/fm1-screen.service`, `console-setup` | Retired console. | Delete. |
| `fleetctl/udev/80-fm1.rules` | **[P1] UDEV-01:** the comment says the rules pin hub vendor, product and bcdDevice at both levels (`:37-44`). The rules match only `KERNELS` + device `idVendor` (`:60-69`), so a hub swap renames slots silently, which is the exact failure the comment says is prevented. Also matches `scsi_generic` with `idVendor=="1209"` (download mode is `4c4a` only). `GROUP="plugdev"` for **every** `1209` USB device (`:86`). | Replace with a generated rule or none (ARCH-02 removes the need). |

### 3.10 `fleetctl/` — retire it

**[P1] FC-01 — `fleetctl` has the bug `fm1-switch` fixed, and addresses by port.**
Evidence: `fleetctl/fleetctl/flasher.py:71-74` builds `argv` with `stdin=subprocess.DEVNULL` and
has no "Nothing written" check. `fm1-switch`'s own test states that DEVNULL "would defeat the
input" (`tests/test_fm1_switch.py:295`), and the handoff records that m-flasher 0.12.1 then exits 0
having written nothing. Writes go to `JobGuard.unit().device_path` (`runner.py:69`, port slot).
The restore guard expects "8 hex digits" (`__main__.py:34`, `flasher.py:99`); real ids are 16.
`pyproject.toml` declares `license = { text = "MIT" }` with no LICENSE file.
What it is still for: nothing that `fm1-switch` does not do better. Salvage: (1) the measured
topology comments in the udev file, moved into a doc as an example; (2) `chip.py`'s known-good
version idea, if `compat.py` wants it; (3) the `JobGuard` "crash lands the unit in a sane
state" principle, already covered by `guided`.
Consolidation plan: (a) move `80-fm1.rules` to `fm1-switch/udev/` as `80-fm1.rules.example`,
or delete it after ARCH-02; (b) `git rm -r fleetctl` on the publication branch with a note in
`docs/history.md`; (c) remove fleetctl from `setup.sh`. Acceptance: `setup.sh` installs one
package; no doc tells a user to run `fleetctl`.
Risks: none to safety. This removes a write path that violates invariants 4, 6, 9 and 10.

---

## 4. Safety-model audit (Appendix A, invariant by invariant)

| # | Invariant | Enforced at | Tested by | Status |
|---|---|---|---|---|
| I-1 | Never guess which unit; unlabelled `?`; provisional rows marked, never authorise writes | `registry.known` `:381-421`; `LiveUnit.label` → `"?"` `:310-312`; `guided.looks_like_flash_id` `:48-52`; `fleet.js:138` `hasFlashId` | `test_a_sticker_is_never_shown_on_two_units`; JS "units without a flash id … cannot be flashed" | **Partial.** Writes: holds. Display: **provisional is never rendered on the panel**. `fleet.js` never reads `u.provisional` **[measured: grep → 0 hits]**, so a power-on-taught binding shows a big letter and RUNNING like a confirmed one (it does show NOT REGISTERED on the button). Port-tie-broken labels are not marked either (§3.5). Unmatched running units are not shown at all (REG-01). |
| I-2 | Two deliberate actions; second names the unit; WRITE far from the opener; a stationary press cannot complete it | `fleet.js` `showConfirm` `:457-465`; server `confirm == unit_id` `web.py:270, 327, 359, 379, 806, 818` | JS "WRITE posts … exactly once"; "FLASH only opens the dialog" | **Violated in two measured layouts** (UX-01, UX-02). Server-side, `confirm` is the same string as `unit_id`, and both are in one payload, so it proves only that the client copied a field. It binds nothing (SEC-01). |
| I-3 | Never write to a unit not in download mode | `guided` writes only to a node from `download_devices()` (`:131-150`), re-checked `:177-179` | `test_target_vanishing_during_restarts_is_refused` | **Holds** in `guided`. **Bypassed** by `fm1-switch install` only to the extent that `/dev/fm1-x` exists only in download mode (udev). That CLI path has no other check (SAFE-01). |
| I-4 | Writes matched by flash id; no match ⇒ nothing; placeholders refused | `guided.py:116-119, 135-171` | 9 tests in `test_guided.py` (ports shuffled) | **Holds** in `guided`. **Violated** by `fm1-switch install` (SAFE-01) and by `fleetctl` (FC-01). |
| I-5 | Nothing silently filtered; warned firmwares reordered and marked | `fleet.js:307-309` sort, `:325` marks | JS "unit view sorts ok, then unknown, then warn" | **Holds** for firmware. GET FIRMWARE silently drops non-installable entries (`web.py:468-469`). That is defensible, but say "N more need their maker's installer". **[P3]** Running units *are* silently filtered (REG-01). |
| I-6 | Back up before every write; failed backup ⇒ nothing written | `guided.py:181-193`; `web.py:274, 336` pass `_backup_before_write` | `test_backup_is_taken_from_the_matched_device_before_the_write`, `test_failed_backup_writes_nothing_and_restarts_the_unit` | **Holds on exit code only** (SAFE-04). `OSError` path escapes (GD-04). CLI install has no backup (SAFE-01). |
| I-7 | One write at a time; refused not queued; polls don't race a write | `lock.acquire(blocking=False)` `guided.py:120, 221`; `/api/fleet` cache `web.py:640` | `test_busy_lock_refuses`, `test_fleet_is_served_from_cache_while_a_write_runs` | **Partial.** Writes are exclusive within one process. Polls race in R1, R3 and R4 (§3.2, SAFE-03). Exclusivity does not extend across processes (CLI). |
| I-8 | Server re-verifies sha256, confirm, path containment | `_check_package` `web.py:181-193`; `_backup_by_id_path` `:340-353`; `_api_flash` name check `:789` | `test_flash_by_id_refuses_hash_drift`, `test_restore_refuses_wrong_unit_bad_checksum_escape_and_missing_confirm` | **Partial.** The sha check is skipped when `sha256` is absent (SAFE-02, measured). Path containment holds **[measured: `../flash.log`, `../../etc/x.bin` → 409]**. Restore re-hashes the image (`:331`) and passes it to m-flasher a few seconds later (TOCTOU window, P3). |
| I-9 | `input="yes\n"`; `_check_written` on every path | `mflasher.py:117, 131` | `test_run_feeds_yes_to_stdin…`, 6 `nothing_written` tests | **Holds for `fm1_switch`** (one `subprocess.run`). **Violated in `fleetctl`** (FC-01). Regex fragility MF-03. |
| I-10 | Every write/refusal/result logged | `_log_flash` `web.py:229-236` | `test_flashlog_lists_finished_records_newest_first` | **Violated** (SAFE-05): pre-guided refusals unlogged [measured]; library removals unlogged; write failures mislabelled; log write failures swallowed (`:235-236`); CLI unlogged. Append-only by convention (`open("a")`), not enforced. |
| I-11 | No debug output to a stdout that is the panel | `Handler.log_message` gated by `FM1_WEB_VERBOSE` `web.py:518-520`; panel is cog, its stdout is the journal (`fm1-kiosk.service`) | – | **Holds [derived].** `serve()` prints one line to the web unit's stdout (journal, not the panel). The retired `fm1-screen.service` was the risky one. Delete it so no one re-enables it. |

### Constructed gaps (each traced through code; none executed against a device)

**G1 — every write path, enumerated [derived].**

| Path | id match | backup | lock | log | sha | confirm |
|---|---|---|---|---|---|---|
| panel/expert `POST /api/flash` | ✔ | ✔ | ✔ | ✔ (partial) | **optional** | same-payload copy |
| `POST /api/restore` | ✔ | ✔ | ✔ | ✔ | image sha ✔ | copy |
| `POST /api/backup` | ✔ (read-only) | n/a | ✔ | ✔ | – | copy |
| `POST /api/restart` | ✔ | n/a | ✔ (busy unset) | ✔ | – | copy |
| `POST /api/backup/delete` | sidecar id ✔ | n/a | ✘ | ✔ | – | copy |
| `POST /api/library/remove` | n/a | n/a | ✘ | **✘** | – | **none** |
| CLI `fm1-switch install` | **✘** | **✘** | **✘** | **✘** | **✘** | **none** |
| CLI `fm1-switch download` | soft key by port map | n/a | ✘ | ✘ | – | – |
| `fleetctl flash/restore` | **✘ (port)** | own | own JobGuard (per-process) | ✘ | ✘ | – |

**G2 — CSRF drive of a write (SEC-01) [measured up to the refusal].** A page on any origin runs
`fetch("http://<pi>:8080/api/flash",{method:"POST",mode:"no-cors",headers:{"Content-Type":"text/plain"},body:JSON.stringify({unit_id:ID,confirm:ID,package:"sloop-2.8.5.fwsc"})})`.
That is a CORS "simple request", so the browser sends no preflight. The server parsed such a
body as JSON from `Origin: http://evil.example` and answered on the merits **[measured: 409
"confirmation does not match" for a deliberately wrong confirm; 409 "that unit is not
registered" for rename]**. `ID` is public in this repo (registry docstring, tests, docs, archives).
The package name is guessable, and `/api/download {id}` can first fetch any catalogue firmware.
`sha256` may be omitted (SAFE-02). Result: a remote page can install any catalogue firmware on
any of the owner's units while the owner browses on the bench Wi-Fi. A backup is still taken
first, so it is recoverable through the panel. Delete-backup and library-remove are reachable
the same way, and delete-backup can remove the recovery images.

**G3 — single-press confirmation (UX-01) [measured, Chromium].** §7.2.

**G4 — wrong-unit write?** I could not construct one through `guided` **[derived]**. The
target is always the node whose freshly read id equals `unit_id`, re-checked after restarts.
The residual risk is m-flasher's `--device` honouring: the code assumes `--device /dev/sgN`
addresses only that node **[assumed; documented on hardware in HANDOFF §13.1-13.2]**.

**G5 — operator misled by a status (UX-03) [derived].** A write succeeds and the unit boots in
30 s, so `_relearn` times out at 25 s and returns `not-running`. `last_event.relearned =
"not-running"`. The unit's fingerprint is not relearned, so the next poll matches nothing and
the unit disappears from the running list (REG-01). The card shows NOT CONNECTED and the unit
screen says "DIDN'T RESTART: POWER-CYCLE IT, OR RESTORE… Switch it off and on again." The unit
is in fact running the new firmware. Power-cycling is harmless. RESTORE overwrites presets and
sequences with the pre-write image. So a lying status can lead to a destructive choice.

---

## 5. Test-suite audit

### 5.1 Baseline [measured]

| Suite | Command | Result |
|---|---|---|
| fm1-switch Python | `.venv/bin/python -m pytest -q` | **170 passed** in 16.53 s |
| fm1-switch JS | `node --test tests/js/fleet.test.mjs` (node v24.22.2) | **34 passed** |
| fleetctl | `.venv/bin/python -m pytest -q` | **47 passed** in 0.72 s |
| fm1-switch without `realtest/` | fresh `git archive` copy, `realtest/` deleted, `PYTHONPATH=.` | **168 passed, 2 skipped** (`test_web_hub.py:207, :218`) |

The handoff's "123 Python tests + 12 JS tests" (`HANDOFF:22`, `:495-496`) is stale.

### 5.2 Coverage map [derived from test names and imports]

| Area | Covered | Not covered |
|---|---|---|
| `guided` flow | thorough (fake bus, shuffled ports, every refusal) | real timing; `OSError` from `before_write` |
| `mflasher` argv/parse/nothing-written | thorough; real 1.3 `info` output pinned | real `firmware`/`restore`/`backup` stdout for 0.12.1 and 1.3 (only the success phrase is pinned, from a commit-era capture **[documented]**) |
| registry/registration | thorough | `scan()` (always empty off-Pi); `known()` with unmatched runs |
| softkey | thorough with fake sysfs | – |
| packages | synthetic + 7 real | – |
| hub | parse, cache, fetch verify (mocked) | `urlopen` failure paths with a stale cache; `force` |
| web API | refusals, caches, backups, rename, prune | concurrency; CSRF/Origin; static traversal (`/../web.py`); `/api/units?probe=1` during a write; `/api/library/remove`; `/api/catalog/refresh` |
| `fleet.js` | 34 behaviour tests | layout, CSS, modal geometry, async-render interleave (JS-01), `provisional` |
| `fleet.css`, `fleet.html` | **none** | everything (§7) |
| `app.js`, `index.html` | **none** | everything |
| `__main__` | reachability, `download` | `install` (the unguarded path), `register` |
| tools | EDID (10 tests) | calibrate, fetch-realtest |
| systemd/udev/shell | none | – |

### 5.3 Fixture provenance

| Fixture | Where | Provenance | Risk |
|---|---|---|---|
| `REAL_INFO_V13` | `tests/test_fm1_switch.py:100-127` | "CAPTURED FROM THE HARDWARE … verbatim" **[documented]** | Low. Keep it byte-exact (a whitespace normaliser in an editor would break it silently). Add a hash assertion of the fixture string. |
| `REAL_INFO_LEGACY` | `:128-` | colon form; provenance not stated | Medium: may be the hand-written fixture that caused the parsing bug. Label it "synthetic, legacy shape". |
| "Done: everything written is verified" / "Nothing written." | `:292`, `:321` | wording consistent with handoff's 2026-11-20 hardware note **[documented]** | Medium: this is the only signal behind invariant 9. Capture full real stdout of a successful and an unanswered `firmware` for both versions. |
| Fake sysfs trees | `test_softkey.py`, `test_guided.py`, `test_registration.py` | hand-built to the documented layout | Medium. The stale `by-path` case reproduces a real bug (`test_softkey.py:171-`). Capture `find /sys/bus/usb/devices/1-1.2 -maxdepth 3` and `ls -l /dev/snd/by-path` from the Pi as golden trees. |
| Synthetic `.fwsc` | `test_web_hub.py:61-` `build_fwsc` | generated by test code using the same algorithm as the parser | High in principle (a parser and a builder sharing an error agree). Mitigated by the 7 real packages. |
| 7 real `.fwsc` | `realtest/` | downloaded from the hub (5), V15 from the hub under another name, SHRUB copied from the Pi | Low for correctness. A licensing question (§10). |
| Catalogue rows | `test_web_hub.py:350-` | hand-written dicts | Medium. Save one real `catalog.json` row per shape (pkg, externalInstall, linkOnly, beta) as a fixture. |
| EDID digest | `test_make_edid.py:108` (md5 `9f50…3868`) | pins the generator's output | It pins what the repo *generates*, not what the Pi *has installed*. The docstring says so (`:103-106`). Fine, and passes **[measured]**. |
| JS fake DOM | `tests/js/fleet.test.mjs:16-35` | hand-written | Catches logic, never geometry (§5.4). |

### 5.4 Missing test kinds and the cheapest honest upgrades

1. **[P1] TEST-01 — layout and geometry in a real engine (dev-only).** Add
   `tests/browser/test_layout.py` using Playwright (CI only, not on the Pi). Run it against the
   stub server in this audit (canned JSON, refuses POSTs; reproduce it under `tests/browser/stub.py`).
   Assertions at 800×480: (a) for every opener→modal pair (FLASH, SEVERAL→FLASH n,
   RESTORE row, DEL, BACK UP), `area(opener ∩ confirm-go) == 0`, plus a 24 px margin; (b) for
   every view, `#screen.scrollHeight == clientHeight` and no element's bottom exceeds `#screen`;
   (c) every button ≥ the agreed tap size (§7.3). Use WebKit (`playwright.webkit`), which is
   nearer to cog's WPE than Chromium. It still differs, so keep one on-panel photo check per
   release (Phase C).
2. **[P1] TEST-02 — invariant regression tests**, one per Appendix A item, named
   `test_invariant_<n>_*`, in one file `tests/test_invariants.py`, so a reader can find them.
   Include: CLI has no unguarded write; empty sha refused; cross-origin POST refused; no
   `m-flasher info` while `APP.lock` is held; log line for every refusal type.
3. **[P1] TEST-03 — concurrency** (CONC-01 acceptance test).
4. **[P2] TEST-04 — end-to-end with a fake device**: a fake `m-flasher` script on `FM1_FLASHER`
   that reads `--device` and `stdin` and writes a backup `.bin`/`.json` to a dir. It also needs a
   fake sysfs root (make `registry._SCSI_GENERIC` and `_USB_DEV_ROOT` overridable by env var,
   like `softkey.USB_SYSFS`). The test drives `/api/flash` over real HTTP. That catches
   wiring bugs that monkeypatching hides.
5. **[P2] TEST-05 — JS async render** (JS-01 acceptance).
6. Keep the JS fake DOM for logic. It is fast, dependency-free and has found real bugs
   (`HANDOFF:507-509`).

---

## 6. Feature inventory and gaps

### 6.1 Inventory

Status values: **HW** = proven on hardware per a dated repo record; **fake** = tested with fakes
only; **missing** = documented but absent.

| Feature | Where | Status | Evidence |
|---|---|---|---|
| Guided flash by flash id (panel) | `guided.py:111-211`, `fleet.js:559-599` | **HW** | HANDOFF §13.1, §13.2 ("panel flash of ChoralRoot onto C … ~32 s") **[documented]** |
| Pre-write auto-backup | `web.py:388-396` | **HW** | HANDOFF §13.2 **[documented]** |
| Restore from backup | `web.py:321-337`, `fleet.js:470-477` | **HW** | HANDOFF §13.2 **[documented]** |
| Restart unit (no write) | `guided.py:214-233` | **HW** (restart of others) | HANDOFF §13.1 **[documented]** |
| Auto-backup pruning (20/unit) | `web.py:404-423` | fake | HANDOFF §13.3 "pruning on the Pi" unproven |
| Flash history on settings | `web.py:209-226`, `fleet.js:533-555` | fake (HW-adjacent) | – |
| Fingerprint relearn after flash | `registry.py:243-267` | **HW** | HANDOFF §13.1 |
| Visible failures (`attention`) | `fleet.js:146-156` | fake | HANDOFF §13.3; no later commit claims HW **[measured: git log]** |
| BACK UP NOW / per-row DEL | `web.py:356-385`, `fleet.js:479-492` | fake | same |
| Unit names + on-screen keyboard | `registry.py:214-240`, `fleet.js:712-775` | fake | same; keyboard added in `94a0442` after §13.3 |
| UPDATE badges | `web.py:443-458`, `fleet.js:245, 288-289, 382-404` | fake | same |
| GET FIRMWARE | `web.py:461-472`, `fleet.js:777-825` | fake | same |
| SEVERAL multi-flash | `fleet.js:601-710` | fake, **and press-through measured (UX-01)** | same |
| Layout "checked at 800x480" | commit `56d35d6` | unknown method | The commit message does not say on-panel or emulated. Treat as emulated. |
| Expert flash by id | `app.js:255-293` | **HW** (D, 11 s) | HANDOFF §13.2 |
| Catalogue browse/download (expert) | `app.js:295-381`, `hub.py` | HW-adjacent | – |
| Package inspection verdict | `packages.py` | fake + 7 real packages | `test_real_packages` |
| Registration by power-on (CLI) | `__main__.py:315-350`, `registration.py` | fake; no record of HW use after console removal | – |
| **Binding a flash id to a sticker** | was `console_ui.py` (deleted `814c556`) | **missing** | `git show 814c556^:fm1-switch/fm1_switch/console_ui.py` lines 540-560 called `reg.bind`; nothing does now **[measured]** |
| Soft-key reach to unit C (no MIDI node) | `softkey.py:76-88` | **contradictory records** | `softkey.py:79-86` says measured working (2026-11-22); HANDOFF §13.3 "still unproven" |
| Save-survival on new chip | `tools/settle-saves.sh` | not run (no record) | – |
| MIDI hosting | – | **out of scope by decision** | `fleetctl/README.md:6-8`, `docs/FM1-fleet-controller-design.md`. Out of scope; not discussed further. |

### 6.2 Owner's bench: remaining friction

1. **[P0] FEAT-01 — bind on the panel.** When a unit is in download mode and has a readable id but
   no sticker, the card shows `?` and the id tail. Add `POST /api/bind {unit_id, slot, confirm}`
   calling `registration.confirm(...)` (which already exists, is tested, and has no caller),
   and a panel flow:
   ```
   [ ? ]  READY TO FLASH  id …0233        tap
   ┌────────────────────────────────────────┐
   │ WHICH STICKER IS ON THIS UNIT?         │
   │ (it is the one whose screen is black)  │
   │  [ A ]  [ B ]  [ C ]  [ D ]            │   ← big, top half
   │                                        │
   │  CANCEL                    SAVE  ←far  │
   └────────────────────────────────────────┘
   ```
   Server: refuse unless the unit is currently in download mode (re-read `download_devices()` +
   `read_id`, under the lock) and the id is real. Log `event="bind"`. Acceptance: a unit can go
   from "never seen" to FLASH-enabled using only the panel. A test covers refusal when the id
   is not in download mode now.
2. **[P1] Registration on the panel (power-on attribution).** `registration.wait_for_arrival` blocks
   up to 120 s. Expose it as a job: `POST /api/register/start {slot}` → `GET /api/register` poll →
   `{state: waiting|arrived|refused, message}`. Panel: SETTINGS → REGISTER → pick sticker →
   "SWITCH ON UNIT B ONLY" with a countdown. Provisional bindings then render `~` (UX-05).
3. **[P2]** Per-unit timings (`info` reads, write time). Record durations in the log (`ms` per
   phase) so the "about 35 seconds each" copy (`fleet.js:709`) becomes a measured average.
4. **[P2]** Save-survival verification: wrap `settle-saves.sh` as a guided "check saves" action, or
   at least document the manual procedure in the README's status section.
5. **[P2]** Unit C's soft-key path: one hardware run settles the contradiction above. Record it in
   the handoff.

### 6.3 A stranger's bench: what installability actually requires

Hardcoded bench assumptions, each with what to do:

| Assumption | Where | Generalisation (stdlib-only) |
|---|---|---|
| Exactly 4 units, A–D | `units.py:21-29`, registry `free_slots`, `fleet.css:82-88` 2×2 grid | `SLOTS` from config (default 4, max 8). The grid adapts: 1–4 → 2×2, 5–6 → 3×2 (cards 91 px tall **[measured at 6]**, under `--tap`), 7–8 → pages. |
| udev port map with measured `KERNELS` | `fleetctl/udev/80-fm1.rules:60-69`, `registry.scan` | Remove the dependency (ARCH-02). Ship only the permissions part of the rule (vendor 4c4a → group). |
| User and home in units | `systemd/*.service`, `tools/calibrate.py:20` | `%h`, `EnvironmentFile`, `install.sh` writes a drop-in. |
| `~/fm1-*` paths | `web.py:50-57, 204-206`, `registry.py:83-85`, `hub.py:92-97`, `library.py:26` | One `~/.config/fm1/config.toml`… but `tomllib` is read-only and 3.11+; use JSON or `configparser`. Keep the env overrides. |
| Panel EDID | `tools/make-edid.py` | Document as "Waveshare 5" HDMI 800×480 (the one we have); other panels need their own". |
| Touch calibration | `tools/calibrate-touch.sh` | Same. |
| Felucca/stock VID:PIDs | `registry.py:490-493`, `softkey.py:68-69` | Config list of `vid:pid[:product]` (REG-02). |
| Pi 3B+ / trixie | docs | State "tested on"; do not claim others. |

**First-run wizard?** A minimal one fits the ethos. When `registry` is empty and no
`config.json` exists, `/fleet` shows a three-step coach screen: (1) "m-flasher found ✓ / not
found ✗" (reuse `cmd_diagnose`), (2) "switch on ONE unit" → registration job, (3) bind (FEAT-01).
Effort ~2 days with tests. More than 8 units: out of scope (USB hub power and enumeration on a
3B+ are the limit, not the software).

### 6.4 Features to add, and features to declare out of scope

| Proposal | Effort | API sketch | Acceptance |
|---|---|---|---|
| Bind on panel (FEAT-01) | 1–1.5 d | `POST /api/bind {unit_id, slot, confirm}` | §6.2.1 |
| Registration job | 1.5 d | `POST /api/register/start`, `GET /api/register`, `POST /api/register/cancel` | unit taught from panel; two arrivals refused with message |
| Unlock token (SEC-01) | 0.5 d | `X-FM1-Token` header; token shown on panel SETTINGS | §9 |
| Config file | 1 d | `~/.config/fm1/config.json` | stranger edits one file, no code |
| Phase timings in log | 0.5 d | `{"phase_ms": {...}}` in `done` records | multi copy uses real average |
| Fake-device e2e harness | 1.5 d | env overrides for sysfs roots + fake m-flasher | TEST-04 |
| **Out of scope:** MIDI hosting (decided); BLE; flashing bootloaders from the panel (`mflasher` exposes `bootloader` in docs but no UI. Keep it CLI-only and documented as expert); multiple Pis; remote access beyond the LAN. | – | – | – |

---

## 7. UX audit

Constraints honoured: 800×480, dpr 1, single resistive touch (a stationary press repeats),
no keyboard, no scroll, cog fullscreen, read at arm's length to 2–3 m, one unit at a time,
buttons over swipes.

**Measurement setup [measured, with caveats].** The real `fleet.html/css/js`, unmodified, served
by a stub that returns canned JSON and refuses all POSTs. Rendered in the desktop app's
Chromium-based browser pane at an emulated 800×480 viewport. Fonts fell back to the Mac's
(DejaVu Sans is not installed here), and the engine is Blink, not WPE. Heights in `vh` are
engine-independent. Text-driven heights (modal body) will differ somewhat on the Pi.
**Every geometry number below needs one on-panel confirmation.** None is a guess, but all come
from a different engine.

### 7.1 Layout arithmetic [measured]

| View | `#screen` | scrollH = clientH | Clipped | Notes |
|---|---|---|---|---|
| fleet (4) | y 55–418 (362 px) | 362 = 362 ✔ | none | cards 382×163 |
| fleet (6) | same | ✔ | none | **cards 382×91: below the 96 px target** |
| unit | ✔ | ✔ | none | PAGE=2 rows of 58 px; actions 68 px |
| multi | ✔ | ✔ | none | |
| restore | ✔ | ✔ | none | RPAGE=3 rows of 58 px |
| name | ✔ | ✔ | none | choice buttons 188×58 |
| type | ✔ | ✔ | none | **keys 72×48** |
| settings | ✔ | ✔ | none | shows 2 history rows |
| get | ✔ | ✔ | none | GPAGE=3 |

So the "no scroll" claim holds in every view at 800×480. The "≥ 96 px target" claim
(`fleet.html:15`, `fleet.css:33`) holds **only for the modal's two buttons**. `--tap` is used
nowhere else (`grep -n "var(--tap)" fleet.css` → `:238` only). Firmware rows are 58 px, actions
68 px, header buttons 56 px, keys 48 px.

### 7.2 Press-through (the invariant-2 check) [measured]

| Opener (rect) | Confirm button (rect) | Overlap | Verdict |
|---|---|---|---|
| Unit: FLASH `[12,338,263,406]` | WRITE `[407,327,733,423]` | **0** | ✔ (FLASH overlaps CANCEL by 13,328 px², which is the right side to overlap) |
| SEVERAL: `FLASH 4 UNITS` `[406,267,788,334]` | WRITE `[407,293,733,389]` | **13,366 px²** | **✘ UX-01, P0** |
| RESTORE: BACK UP `[603,338,788,405]` | BACK UP (go) `[407,293,733,389]` | **6,630 px²** | **✘ UX-02, P1** (copy only, but it soft-keys units) |
| RESTORE: DEL `[700,198,788,256]` | DELETE `[407,261,733,357]` | 0 | ✔ (margin 5 px vertically, too close) |
| RESTORE: row `[12,198,692,256]` | WRITE `[407,328,733,424]` | 0 | ✔ |

**[P0] UX-01 — SEVERAL's `FLASH n UNITS` is under WRITE.**
Evidence: `fleet.js:688-691` (primary is the right-hand of two `.actions` buttons). The modal is
centred (`fleet.css:214-221`) and its go button is the right-hand of two (`fleet.html:42-45`).
The table above, plus a screenshot taken during the audit.
Why: a resting finger that taps `FLASH 4 UNITS` at e.g. (600, 310) is inside WRITE when the
repeat fires. One press writes four units.
Fix: put WRITE at the **opposite vertical end** from every opener, not just the opposite
horizontal side. Concretely: (a) the modal's actions row goes at the **top** of the box when the
opener is in the bottom half of the screen (all openers today), with the WRITE button at the
far left/right **opposite** the opener's centre-x; (b) the modal ignores clicks for 600 ms after
it opens (`state.armedAt = performance.now()`; `doWrite` returns if `now - armedAt < 600`) and
requires the press to start inside the modal (`pointerdown` recorded on `#confirm-go`, `click`
honoured only if `pointerdown` was on it). (b) is the robust fix, because it does not depend on
geometry. Do both.
Acceptance: TEST-01(a) zero overlap with a 24 px margin for all five pairs; a JS test proves a
click on `#confirm-go` with no preceding `pointerdown` on it does nothing; one on-panel test with
a stylus held on `FLASH n UNITS` for 3 s writes nothing.
Risks: invariant 2 itself. (b) adds a deliberate delay, so copy should say "WRITE (wait)" for
the 600 ms. Trade-off: a slightly slower confirmation in exchange for a guarantee.

**[P1] UX-02 — BACK UP overlaps its own confirm.** Same fix.

### 7.3 Per-screen findings

| Screen | Works | Breaks / ambiguous | Sev | Recommendation |
|---|---|---|---|---|
| **Fleet** | Big letters (16vh), state in words, id tail, orange for attention | Provisional bindings look identical to confirmed ones (I-1). `RUNNING` vs `READY TO FLASH` colour logic: `stateClass` makes any `attention` unit orange "ready" (`fleet.js:219`), so a failed unit and a ready one share a colour. 5+ units shrink cards below the target **[measured]**. Summary shows "N READY" *or* "N RUNNING", never both (`:122`). | P1 | Add `~` after the letter and dim it for provisional/hinted rows; separate colour for attention (deep orange + "!" icon) vs ready (amber); summary "2 RUNNING · 1 READY · 1 OFF". |
| **Fleet (empty)** | Says something | "Tap SETTINGS to register one", and SETTINGS has no registration **[measured]** | **P0 (OSS)** | First-run coach (§6.3). |
| **Unit** | Two rows per page avoids mis-taps; clearing the selection on page turn (a real-incident fix); sorted with marks; FLASH disabled until selected | Marks `!`, `x`, `?` are unexplained at 2–3 m (`:325`); `x` reads as "forbidden", contradicting "warn, never block". PREV/NEXT with N pages and no jump (`:338-346`); with 9 packages that is 5 pages. Port shown to the operator (`:286`), but it is meaningless to them. NAME… and UPDATE are 56 px. | P2 | Replace marks with words in the row ("SAVES LOST", "NOT CHECKED"); show "page 2/5" in big type; group by compat into pages ("OK 1/3"); drop the port from the head. |
| **Confirm (flash)** | Names unit and package; warns on chip; "not erased" note | `p.name` includes `.fwsc`; long names wrap; WRITE (orange) and CANCEL (grey) equal width | P2 | Strip `.fwsc`; make CANCEL the larger target. |
| **Multi** | One confirmation lists all targets; stops at first failure | UX-01; "about 35 seconds each" is unmeasured copy (`:709`); no per-unit progress list after completion beyond one footer line | P0/P2 | UX-01 fix; show a result list `A ✓ B ✓ C ✗ (reason) D –`. |
| **Restore** | Separate screen; ★ for hand-made; disk space | UX-02; the date shows `MM-DD hh:mm` without the year; the DEL target sits 5 px from DELETE; JS-01 duplicates rows **[measured]** | P1 | Fixes above; move DEL into the confirm ("RESTORE / DELETE THIS BACKUP?" chooser) so one row is one tap target. |
| **Settings** | Shows recent activity | Only 2 rows (`:550`); idle copy wrong (`:129`); no REGISTER, no diagnose, no "what is this Pi" (IP, m-flasher version) | P1 | Settings becomes a menu: REGISTER · GET FIRMWARE · HISTORY · ABOUT (IP, version, token). |
| **Name / Type** | Picks before typing; keyboard limited to allowed chars | Keys 72×48; `SPACE`/`⌫`/`SAVE` share the action row with CANCEL; SAVE has no confirm (fine) | P3 | Acceptable for a rare action; note in docs. |
| **Get** | Shows stars and beta | No author or attribution (HUB-03); the empty state conflates "hub unreachable" and "nothing new" (`:789`) | P1 | Separate messages; author per row. |

### 7.4 Feedback and timing

* Busy phase: footer and grey button carry the server's phase text, polled at 1 s while this panel
  writes (`fleet.js:835`). Another client's write is noticed only by the 5 s fleet poll, and only
  on the fleet view (`:836`). On the unit view a phone-initiated write is invisible until the
  operator taps something. **[P2]** Poll `/api/fleet` (cheap, cached) every 2 s on every view.
* The 5 s fleet poll also calls `/api/library` (`:87`), which hashes every package
  uncached (§3.6) **[derived]**.
* Auto-reload on build change is guarded against writes and open dialogs ✔.

### 7.5 Error and edge states (what the operator sees) [derived unless marked]

| Situation | What the panel shows | Problem |
|---|---|---|
| Empty library | Unit screen: no rows, FLASH disabled, footer "TAP A FIRMWARE, THEN FLASH" | Nothing says "the Pi has no firmware: SETTINGS → GET FIRMWARE". P2 |
| Hub unreachable | GET FIRMWARE: "NOTHING NEW TO FETCH (OR THE HUB IS UNREACHABLE)" | Ambiguous. P2 |
| Write fails mid-run | "FAILED: m-flasher exited N: …" (raw stderr, may be long), unit stays in download mode → RESTART UNIT offered | No mention of the backup just taken; log says `refused` (GD-02). P1 |
| Two clients | Second client: 409 "another flash is already running"; panel `last_event` overwritten (R2) | P2 |
| Unit never returns | "DIDN'T RESTART: POWER-CYCLE IT, OR RESTORE…" | Can be false (G5/UX-03). P1 |
| Server down | cog shows its error page; ExecStartPre waits 90 s only at kiosk start | After start, a crashed server leaves the panel on a stale page whose fetches fail. Footer "Startup problem" appears only at load. P2: show "PI SERVICE NOT RESPONDING" on fetch failures. |

**[P1] UX-03 — "DIDN'T RESTART … OR RESTORE" must not advise a destructive action on a guess.**
Fix: only say DIDN'T RESTART when the target port shows a **download-mode** id (the unit is
provably still in download mode) or is empty. If relearn returned `not-running` but a running
FM-1 appeared on that port later, say "BACK ON NEW FIRMWARE — TAP TO RE-IDENTIFY". Never
suggest RESTORE in an automatic message; offer RESTORE only from the unit screen.
Acceptance: JS tests for `relearned ∈ {not-running, not-saved}` with the unit later present →
no RESTORE wording. Server returns `relearned:"late"` per GD-03.

**[P2] UX-05 — render provisional and hinted labels.** `A~` in dim amber; tap → "LABEL IS A GUESS:
PUT IT IN DOWNLOAD MODE TO CONFIRM" (with RESTART/BIND flows). Server adds `"hint": "power-on"|"port"|null`.

### 7.6 Cold start

A new operator powers the Pi with four units connected. Today **[derived]**: the registry is
empty, so `known()` returns nothing for running units. On a stranger's bench there is no udev
map, so download-mode units are not shown either. The screen reads "No units seen yet. Tap
SETTINGS to register one." SETTINGS shows "NO ACTIVITY RECORDED YET" and GET FIRMWARE. **Dead end.**
The registration rule (power on one at a time) appears only in CLI output and in a doc about a
UI that no longer exists.

Proposed first-run coach (all copy at 28 px, one instruction per screen):

```
┌ FM-1 FLEET ───────────────────────── SETUP 1/3 ┐
│  SWITCH EVERY FM-1 OFF.                         │
│  The Pi learns one unit at a time.              │
│                                                 │
│  units on USB now: 0  ✓                         │
│                                       [ NEXT ]  │
└─────────────────────────────────────────────────┘
┌ SETUP 2/3 ── WHICH STICKER? ───────────────────┐
│  [ A ]   [ B ]   [ C ]   [ D ]                  │
│  then switch ON only that unit.                 │
│  watching… 87 s                     [ CANCEL ]  │
└─────────────────────────────────────────────────┘
┌ SETUP 3/3 ── CONFIRM UNIT B ───────────────────┐
│  Seen on port 1-1.2 (Felucca).                  │
│  To make B flashable the Pi reads its flash id  │
│  once (≈5 s, writes nothing).                   │
│  [ CANCEL ]                       [ READ ID ]   │  ← READ ID top-right in real layout
└─────────────────────────────────────────────────┘
```

READ ID = soft key → `read_id` → `registration.confirm` → restart, under the lock. That is a
`guided`-style job with a read-only writer: `guided.flash(..., writer=bind_and_restart)`, the same
trick `backup_unit_now` uses.

### 7.7 Expert UI

Its unit list is the port-slot view (`/api/state`, `web.py:697-705`). "Unit A" there means "the
unit on the port udev calls fm1-a", while the panel's "Unit A" means "the unit wearing sticker
A". **[P1] UX-06**: the same letter means two different things in two UIs. The write is still
id-matched (`app.js:276-282`), so it lands on the device the user saw, but the confirm dialog
names it by the port letter. Its copy is stale: "Slots follow physical USB ports, so a unit
stays 'A' wherever it is plugged" (`index.html:28-32`, self-contradictory);
"hold OCT−" (`index.html:29-30`, `app.js:186-191`), which the handoff calls wrong (`HANDOFF:470`).
Recommendation: **keep** catalogue browsing, package inspection cards, library management,
download. **Drop** the unit list and the expert flash, or rebuild them on `/api/fleet` with sticker
labels. Two UIs that both flash means two confirmation models to keep safe.
Effort: drop 0.5 d; rebuild 1.5 d.

---

## 8. Documentation audit

### 8.1 Drift table (verified)

| Claim | Where | Reality | Evidence |
|---|---|---|---|
| "stdlib only, no daemon, no state file" | `fm1-switch/README.md:10` | daemon (threaded HTTP server, systemd), state in `~/fm1-units.json`, `~/fm1-flash.log`, `~/fm1-hub/` | `web.py:870-880`, `registry.py:83-85` |
| "No network fetch" | `fm1-switch/README.md:94` | catalogue and package fetch | `hub.py:297-355, 397-455` |
| "install fm1-a …" as the way to flash | `fm1-switch/README.md:63` | unguarded (SAFE-01) | `__main__.py:61-81` |
| Bookworm | `fm1-switch/README.md:29`, `setup.sh` | trixie on the Pi | HANDOFF / work order |
| "not a git repository" | `HANDOFF:71-73` | git repo, 28 commits | `git log` |
| "123 Python + 12 JS" | `HANDOFF:22, 495-496` | 170 + 34 | §5.1 |
| §13.3 "NOT yet run on hardware" | `HANDOFF:718` | still true per git; `56d35d6` says "checked at 800x480", method unstated | `git log -4` |
| Soft key is a broadcast | `guided.py:17-20`, `softkey.py:38-48`, `__main__.py:241-252` | pointed in all recorded runs since 2026-11-22 | `HANDOFF:468, 709-714` |
| "writes are addressed per slot … `/dev/fm1-<letter>`" | `softkey.py:47-48`, `web.py:14-15`, `app.js:6-9` | by flash id | `guided.py` |
| "Reads of unit state take the same lock" | `web.py:21-23` | they do not | §3.2 |
| "we show author + repo on every card" | `hub.py:13` | author only, expert UI only | HUB-03 |
| udev rules pin hub models | `80-fm1.rules:37-44` | they do not | UDEV-01 |
| "m-flasher … already enforces … read-back verify" | `fm1_switch/__init__.py:3-6` | not verified here | – |
| "agreed with the USB descriptor serial" | `archives/units/REGISTRY.md:9-10` | all units report iSerial 0 | `HANDOFF:471` |
| Console/TUI keys A–D/N/L/R, tty1 | `docs/FM1-fleet-manager-screen.md` (throughout) | console deleted in `814c556` | git |
| "Hold OCT−" for download mode | `index.html:29-30`, `app.js:186-191`, older docs | wrong per HANDOFF | `HANDOFF:470` |
| "fleetctl status/backup" as next steps | `fleetctl/install.sh:68-79`, `fleetctl/README.md` | fleetctl superseded | §3.10 |
| restore `--id` "8 hex digits" | `fleetctl/__main__.py:34` | ids are 16 hex | registry docstring |
| tools/fetch-realtest "five" packages, `archives/FM1-firmware-install-model.md` | `tools/fetch-realtest.sh:2, 9` | seven; file is in `docs/` | `ls` |
| "Uses the cached catalogue only" (updates) | `HANDOFF:742-743` | true for `update_for`; false for `compat_for` | HUB-01 |

### 8.2 Disposition

| File | Current? | Audience | Bench-private content | Disposition |
|---|---|---|---|---|
| `docs/HANDOFF-fleet-manager.md` | mostly (stale counts, git claim) | owner/devs | **yes**: LAN IPs (`:57, :69, :456`), hostname ×15, owner username ×25, SSH key path and commands (`:58, :77-78, :582-612`), sudoers path, Pi home paths | **Move out of the public repo** (private `bench/` repo or a gitignored `docs/private/`). Publish a sanitised `docs/architecture.md` extracted from §6–§8, §10, Appendix C. |
| `docs/FM1-fleet-manager-screen.md` | **no** (retired console) | operator | hostname, unit ids | **Retire with a banner**; salvage the identity/registration concept text into `docs/identity.md`. |
| `docs/FM1-fleet-web-ui.md` | partly (2026-11-21, pre-guided) | operator | LAN IP (`:16`), hostname, username | **Merge** into `docs/expert-ui.md` after UX-06; sanitise. |
| `docs/FM1-firmware-install-model.md` | yes (evidence doc) | devs/users | unit ids | **Keep, sanitise ids** ("unit A (old chip)"). Valuable to strangers. |
| `docs/FM1-firmware-landscape.md` | dated research | users | none found | **Keep** under `docs/background/` with "as of 2026-11-19". |
| `docs/FM1-fleet-controller-design.md` | historical | devs | – | **Keep as history** (`docs/history/`), banner "superseded by architecture.md". |
| `docs/FM1-h4midi-wc-integration.md` | out of scope | owner | – | **Move out** (MIDI routing is out of scope), or `docs/background/`. |
| `docs/FM1-pi-appliance-assessment.md` | historical | owner | – | `docs/history/` with banner. |
| `docs/FM1-pi-arrival-runbook.md` | partly (611 lines, bench-specific, unit ids ×8) | owner | yes | **Move out**; derive a public `docs/install.md` from its generic parts. |
| `fm1-switch/README.md` | **no** | stranger | – | **Shrink and link**: 15 lines pointing to the top README; remove `install`. |
| `fleetctl/README.md` | no | – | – | Retire with fleetctl. |
| `archives/units/REGISTRY.md`, `backups/README.md` | owner data | owner | **yes**: ids, dump hashes | Leave the repo with `archives/`, `backups/`. |
| *(missing)* top-level `README.md` | – | stranger | – | **Create** (Phase A). |

### 8.3 Proposed public `docs/`

```
README.md                    what / status / install / safety / credits / scope
CONTRIBUTING.md
LICENSE
docs/
  index.md                   one line per file
  install.md                 fresh Pi → working panel (generic)
  identity.md                why flash ids; registration; bcdDevice hint
  safety.md                  the invariants, each with its enforcement site
  architecture.md            module map, API table (from §2 of this audit)
  firmware-install-model.md  sanitised evidence doc
  hardware-notes.md          the panel, EDID, touch; "tested on" list
  background/firmware-landscape.md
  history/  (controller-design, appliance-assessment, fleet-manager-screen — bannered)
  AUDIT-fleet-manager.md     this file
```

**Top-level README must contain:** one-paragraph what/why; photo/screenshot placeholders
(owner TODO); a status table ("proven on hardware" vs "tested with fakes", from §6.1); a 2-minute
architecture sketch; install quickstart with every bench assumption listed (§6.3), and the
FEAT-01 limitation stated plainly until it is fixed; the safety model in eight lines; credits
(m-flasher, The Firmware Hub with its address, licence and terms, firmware makers); scope (no
MIDI hosting); and "what this will never do" (guess a unit, write without a confirmation that
names it, write without a backup).

---

## 9. Security and threat model

### 9.1 Threat model (explicit)

* **Asset:** the flash contents of each synth (firmware and the owner's presets and sequences),
  and the backups that are the only undo (`web.py:388-401`; flashing merges, HANDOFF).
* **Irreversible-ish action:** write or restore (recoverable from a backup), and **deleting
  backups** (not recoverable).
* **Actors:** (A1) the operator at the panel; (A2) anyone on the bench Wi-Fi; (A3) any web page
  the operator, or anyone on the LAN, visits in a browser; (A4) a malicious catalogue entry or
  package host; (A5) a local user on the Pi.
* **Out of scope:** physical access (it can unplug or reflash anything); compromise of m-flasher.

### 9.2 Findings, ranked

**[P0] SEC-01 — CSRF and DNS rebinding: any web page can drive writes and deletions.**
Evidence: `web.py:537-547` (any Content-Type parsed as JSON); `:597-627` (no Origin/Host/token
check); measured: cross-origin `text/plain` → body honoured (§4 G2); `Host: attacker.example` → 200 on
`/api/fleet` (so a DNS-rebinding page can also *read* ids). Unit ids are public in the repo.
Why: A3 can write firmware to the owner's units and delete their backups without being on the LAN.
Fix (cheap, layered, stdlib):
1. Refuse POST unless `Content-Type` is exactly `application/json` (this forces a CORS preflight,
   which the server never answers with `Access-Control-Allow-*`). That blocks the simple-request path.
2. Refuse POST when `Origin` is present and not in an allowlist (`http://127.0.0.1:8080`,
   `http://localhost:8080`, plus configured LAN names).
3. Refuse any request whose `Host` is not in the same allowlist or an IP literal of this Pi.
   That blocks DNS rebinding.
4. For LAN clients (not `127.0.0.1`): require `X-FM1-Token: <6-digit code>`, which the panel
   displays on SETTINGS → ABOUT and the expert UI asks for once (kept in `sessionStorage`).
   The panel itself, on loopback, needs no token.
Acceptance: TEST-02 cases. A `text/plain` POST → 415. A foreign Origin → 403. A foreign Host → 421.
A LAN POST without the token → 401. The panel path is unchanged. Risks: touches invariant 2
(confirmation) only by adding a factor. It must not replace the modal.

**[P1] SEC-02 — default bind `0.0.0.0`, no authentication.**
Evidence: `web.py:870`, `__main__.py:103`, `fm1-web.service:10`.
Why: A2 can do everything A1 can, from a phone, including deleting backups.
Fix: default `127.0.0.1`. The LAN expert UI becomes opt-in (`--host 0.0.0.0` or config) and
requires the SEC-01 token. Acceptance: fresh install serves only loopback; README explains opting in.
Trade-off: the owner's phone workflow needs one config line.

**[P2] SEC-03 — static serving leaks `fm1_switch/web.py`.**
Evidence: `web.py:846-849` checks `str(target).startswith(str(STATIC_DIR.resolve()))`. `…/fm1_switch/web.py`
starts with `…/fm1_switch/web`. Measured: `GET /../web.py` (path-as-is) → **200, 37,450 bytes,
`text/x-python`**; `GET /..%2fweb.py` → 200; `GET /../registry.py` → 404.
Why: source disclosure only, of a file that will be public anyway. It is a correctness bug in a
security check.
Fix: `if STATIC_DIR.resolve() not in target.parents: 404` (as `_backup_by_id_path` already does), and
serve only an allowlist (`fleet.html`, `fleet.css`, `fleet.js`, `index.html`, `style.css`, `app.js`).
Acceptance: test `/../web.py` → 404.

**[P2] SEC-04 — side effects on GET and unbounded files.**
`/api/viewport` appends attacker-controlled text to `/tmp/fm1-viewport.log` with no size cap
(`web.py:557-571`); `/api/units?probe=1` runs device reads (SAFE-03). Fix: make viewport a POST,
cap it at 64 KB with rotation, and make `probe` a POST under the lock. **[measured: one GET created
the file on this Mac; I deleted it afterwards.]**

**[P2] SEC-05 — file permissions.** No `umask`/`chmod` anywhere in `fm1_switch` **[measured: grep]**.
The flash log, registry and backups are created 0644, i.e. world-readable (measured on this Mac:
`-rw-r--r--` flash log). Backups are whole-flash images. Fix: `UMask=0077` in `fm1-web.service`, and
`os.umask(0o077)` in `serve()`. Acceptance: new files are 0600.

**[P2] SEC-06 — catalogue trust (A4).** Packages are sha256- and size-verified against the
catalogue (`hub.py:445-452`) ✔, over HTTPS to a fixed host ✔. The filename comes from the
catalogue (`hub.py:411-413`, `rsplit("/")`): a hostile catalogue can overwrite an existing library
file of the same name, but cannot escape the directory (`/` is split away; `..` becomes `...fwsc`
at worst) **[derived]**. Fix: refuse to overwrite an existing file whose sha differs, and write to
`<id>-<version>.fwsc`.

**[P3] SEC-07 — subprocess and paths.** `subprocess.run(argv list)` with no shell
(`mflasher.py:108`) ✔. The package path comes from the library dir with `/` and `\` refused
(`web.py:789`) ✔. Restore and delete paths are contained (measured) ✔. sysfs `realpath` is
read only (`registry.py:329`) ✔. udev symlinks are resolved with `realpath` before use
(`units.py:57`). A user who can write `/dev` could redirect them, but that is root ✔.

**[P3] SEC-08 — narrow sudoers.** `/etc/sudoers.d/010_fm1` is documented (`HANDOFF:151, 449`) but
not in the repo, so its shape cannot be audited here. Publish an example in `docs/install.md` that
grants exactly `systemctl restart fm1-kiosk.service` and nothing with a wildcard.

### 9.3 Fix before publication vs accept

| Fix before publishing | Document as accepted risk for a bench tool |
|---|---|
| SEC-01 (all 4 layers), SEC-02, SEC-03, SEC-05 | Anyone with physical access can do anything |
| | A LAN peer **with** the token can flash (that is the feature) |
| | Catalogue trust = trust in the hub and HTTPS |

---

## 10. Open-source readiness

### 10.1 Licensing analysis (decision is the owner's)

**The code.** All 28 commits are by the owner, co-authored with Claude **[measured: git log]**. No
third-party code is vendored. The owner can license it freely. **No LICENSE exists, and
`fleetctl/pyproject.toml` already declares `license = { text = "MIT" }`**. That is an
inconsistent, unaccompanied declaration and must be reconciled with whatever the owner chooses.

| Option | One-line trade-off |
|---|---|
| **MIT** | Shortest, most permissive, widely understood. No patent grant, no requirement to share changes. |
| **Apache-2.0** | Permissive, with an explicit patent grant and NOTICE handling. Longer; a fine fit for hardware-adjacent tools. |
| **GPL-3.0-or-later** | Derivatives must stay open. Discourages closed forks of a flashing tool. Some users and companies avoid it. |

**m-flasher.** Invoked as a separate executable via `subprocess` (`mflasher.py:90-132`), not linked
or imported, so its licence does not propagate to this code under any of the three options. That
is the general understanding of process boundaries, not legal advice. **Its licence is not
verified here (offline).** The docstring links `github.com/jvitkauskas/m-flasher` (`mflasher.py:7`).
Owner TODO: read its LICENSE and credit it in the README accordingly. Do not redistribute its
`.deb` in this repo (it is not here today ✔).

**The Firmware Hub catalogue.** CC BY 4.0 for the compilation. Terms as recorded in `hub.py:6-21`:
name the hub with its address; credit makers and link their repos; read at most once a day with an
identifying User-Agent; install only sha-matched packages. The code honours attribution (panel: no;
expert: yes), the UA (`hub.py:63`, which says "personal use", wrong once public), and sha matching ✔.
It does not honour repo links (HUB-03) or rate-limit forced refresh (HUB-02). The repo does not
redistribute the catalogue ✔ (`realtest/.catalog.json` is written by the fetch script but is not
tracked **[measured: git ls-files]**).

**Firmware binaries in `fm1-switch/realtest/` (7 files, 4.4 MB, tracked since the first commit).**
Community firmwares (SLOOP, Felucca, OberVave, Melodee, ChoralRoot, SHRUB) by individual makers
with mostly unstated redistribution terms, plus **V15, M-VAVE's own stock firmware** (vendor
copyright). SHRUB is "not on the hub" (`tools/fetch-realtest.sh:8`), so its provenance is the
least clear. **Recommendation: remove from the public repo.** The tests already skip without them
**[measured: 168 passed, 2 skipped]**, and `tools/fetch-realtest.sh` already fetches 6 of 7 by
catalogue hash.

**`archives/` and `backups/` (30 files, ~20 MB).** Whole-flash dumps of the owner's four units
(containing factory unit ids and the stock vendor firmware), a factory-settings dump and a
"transporter" dump (provenance: `prepare-archives.sh:24`, a third party's backup repo, licence
unknown), V15 again, and tarballs of all of it. Not redistributable (vendor firmware, third-party
dump) and identifying (unit ids). **Recommendation: untrack, and keep them out of the published
history** (§10.3).

### 10.2 Exposure sweep [measured: `git grep` on HEAD, `git log --all -p`]

| Item | Tracked locations (HEAD) | In history |
|---|---|---|
| Private LAN addresses | `docs/FM1-fleet-web-ui.md:16`; `docs/HANDOFF-fleet-manager.md:57, 69, 456` | yes (4 added lines) |
| Hostname (`*.local`) | HANDOFF ×15, `FM1-fleet-web-ui.md`, `FM1-fleet-manager-screen.md` | yes |
| Owner's Pi username / home | HANDOFF ×25, `FM1-fleet-web-ui.md`, `FM1-pi-arrival-runbook.md`, all 3 systemd units, `tools/calibrate.py:20` | yes |
| SSH key path + ssh/scp commands | HANDOFF `:58, :77-78, :582-612` | yes |
| sudoers file name | HANDOFF `:151, :449` | yes |
| Unit flash ids (4) | `registry.py` docstring, 5 test files, 4 docs, `verify-units.sh`, `archives/units/*` | yes |
| Mac username | none in HEAD | **yes**: the stray `fm1-switch/~/fm1-hub/inspect.json` (added in `018b847`, removed in `8d3e782`) contains pytest temp paths that include the Mac account name |
| Author e-mail | not in files | **every commit** carries the owner's personal e-mail as author |
| Secrets (private keys, tokens, passwords) | none found | none found (pattern sweep for `PRIVATE KEY`, `ghp_`, `AKIA`, `passw…`: only test strings like `../etc/passwd`) |
| `.shot/` browser profile | not tracked ✔ (`.gitignore:6`) | never tracked ✔ |
| Remote | `git remote -v` → **none** ✔ | – |
| Large blobs | realtest 7 × ~0.6 MB; archives/backups ~16 MB | all since `018b847` |

Unit ids in tests are fixtures. Replacing them with obviously synthetic ids (`aaaaaaaa00000001`…)
is a Phase B change to test files, not Phase A. That removes them from HEAD; history needs §10.3.

### 10.3 History strategy (owner decides)

Untracking (`git rm --cached`) removes files from *future* commits only. Every past commit still
contains the dumps, V15, the realtest binaries, the stray file, LAN details and the author e-mail.

| Option | What it does | Pros | Cons |
|---|---|---|---|
| **A. Fresh orphan history for publication** (recommended) | `git switch --orphan public` from the sanitised tree, one initial commit (optionally with a chosen author identity); keep the current repo private as the archive | Simplest to reason about; nothing leaks; private history kept intact | Public repo loses development history (link the audit and a `docs/history.md` summary instead) |
| B. `git filter-repo` on a copy | Rewrite all commits to drop paths and replace strings/authors | Keeps history | Easy to miss something (strings in docs across 28 commits); rewritten hashes break references in docs; needs a tool not installed here |
| C. Publish as is | – | None | Publishes the owner's dumps, vendor firmware, third-party binaries, LAN details, e-mail. **Not recommended.** |

I will not rewrite history in Phase 3 under any option. A and B are owner actions.

### 10.4 Hygiene checklist (exact steps)

1. Owner picks a licence → `LICENSE` at the root; `fm1-switch/pyproject.toml`: `license = "<SPDX>"` (setuptools ≥ 77 accepts SPDX strings; the build-system pin `setuptools>=61` would need raising, or use `license = { text = "<SPDX>" }` with `license-files`), `authors`, `readme`, `classifiers` (`Programming Language :: Python :: 3`, `Operating System :: POSIX :: Linux`, `Topic :: System :: Hardware`, `Environment :: Web Environment`, licence classifier), `[project.urls]`.
2. `git rm -r --cached archives backups` (and `fm1-switch/realtest/*.fwsc` if agreed); `.gitignore` += `archives/`, `backups/`, `fm1-switch/realtest/*.fwsc`, `fm1-switch/realtest/.catalog.json`, `AUDIT-PROMPT.md` (owner's call), `node_modules/`, `.DS_Store` (present).
3. Docs per §8.2. Bench-private docs go to a gitignored `docs/private/` (moved, not deleted).
4. Templated systemd units are code-adjacent (`systemd/` is out of Phase 3 scope by the work order), so Phase B.
5. `.github/workflows/ci.yml`: Python 3.10 and 3.13 × {fm1-switch, fleetctl if kept}, `pip install pytest`, `pip install -e .`, `pytest -q`; Node 20 `node --test fm1-switch/tests/js/fleet.test.mjs`. Must pass with `realtest/` absent (it does: 168+2 skipped).
6. `CONTRIBUTING.md`.
7. Package READMEs shrunk and linked.
8. Repo name: `fm1-fleet-manager` (descriptive; "M-VAVE" and "FM-1" are vendor names. Use them descriptively and say "not affiliated with M-VAVE").
9. Screenshot/photo placeholders → owner TODO (requires the panel).

### 10.5 Fresh-Pi quickstart, mentally executed today [derived]

1. Flash Raspberry Pi OS Lite 64-bit; `git clone`; `sudo ./setup.sh`. **Trips:** installs fleetctl
   too; needs `MFLASHER_DEB` or a pre-installed m-flasher; installs a udev rule with this bench's
   `KERNELS` paths (`setup.sh` step 6); no `curl` for the kiosk's `ExecStartPre`.
2. Install systemd units. **Trips:** user, home and paths are hardcoded; the web unit is a *user*
   unit that needs lingering enabled (not documented generically).
3. Panel. **Trips:** an EDID and calibration for one specific Waveshare panel; on other panels
   `tools/make-edid.py` is wrong by construction.
4. Open `/fleet`. **Trip (blocking):** empty fleet; no registration or binding path (FEAT-01); unmapped
   ports mean download-mode units are not listed (ARCH-02).
5. Flash. **Cannot reach it from the panel.** The only route that works on an unmapped bench is the
   unguarded `fm1-switch install` (SAFE-01), and only after `label-units.sh`.

**Honest answer: a stranger cannot get from clone to a panel flash today.** FEAT-01 and ARCH-02 are
the minimum for "install it on their own bench".

---

## 11. Prioritised roadmap

### Phase A — repo prep (mechanical; implemented after the owner's checkpoint, on `audit/oss-prep`)

Each item is one commit. **No `fm1_switch/*.py`, `web/`, `tests/` assertions, `tools/`,
`systemd/` or `fleetctl/` code changes.**

1. `LICENSE` (owner's choice) + licence/authors/classifiers/urls in `fm1-switch/pyproject.toml`.
   `fleetctl/pyproject.toml`: I recommend retiring fleetctl (FC-01), so I will reconcile its
   existing `MIT` declaration with the chosen licence and touch nothing else, unless the owner says otherwise.
2. Top-level `README.md` (§8.3 contents; owner TODO placeholders; honest status; FEAT-01 stated as a
   current limitation; SAFE-01 warned against until fixed).
3. Docs: `docs/index.md`; supersession banners (FM1-fleet-manager-screen, controller-design,
   appliance-assessment, fleet-web-ui); verified factual corrections from §8.1 in the docs that stay
   public; bench-private docs moved to a gitignored `docs/private/` (moved, not deleted).
4. `git rm --cached` `archives/`, `backups/` (+ `realtest/*.fwsc` if agreed); `.gitignore`. Tests
   already skip without realtest, so no test change is needed **[measured]**.
5. Sanitise personal references in remaining tracked **prose** (docs, script comments) per §10.2.
   Shell scripts that hardcode bench values I will annotate, not rewrite, if the change would
   alter behaviour. Unit ids inside `registry.py`'s docstring and test fixtures are code/tests:
   Phase B.
6. `.github/workflows/ci.yml` (§10.4.5).
7. `CONTRIBUTING.md`.
8. Shrink `fm1-switch/README.md` (and `fleetctl/README.md` → retirement banner) to match reality.

### Phase B — developer workstreams (priority order)

| # | Workstream | Findings | Effort | Acceptance |
|---|---|---|---|---|
| B1 | **Confirmation that cannot be pressed through** | UX-01, UX-02 | 0.5 d + TEST-01 1 d | §7.2 acceptance; Playwright geometry suite in CI |
| B2 | **API hardening** | SEC-01, SEC-02, SEC-03, SEC-04, SEC-05 | 1.5 d | §9 acceptance; TEST-02 security cases |
| B3 | **Close the CLI bypass; retire fleetctl** | SAFE-01, FC-01, UDEV-01 | 1 d | §3.7, §3.10 acceptance |
| B4 | **Bind + registration on the panel; first-run coach** | FEAT-01, §6.2.2, §7.6 | 3 d | a blank bench reaches FLASH from the panel only |
| B5 | **One identity path (sysfs) and visible unknowns** | ARCH-02, REG-01, REG-02, UX-05 | 1.5 d | §3.5 acceptance |
| B6 | **Write-path integrity** | SAFE-02 (require sha), SAFE-04 (verify backup sidecar + sha), MF-01, MF-03, GD-02, GD-04 | 1.5 d | invariant tests 6, 8, 9 |
| B7 | **Concurrency** | CONC-01, SAFE-03, R1–R7, PKG-01 | 1.5 d | CONC-01 test |
| B8 | **Honest audit log** | SAFE-05 | 0.5 d | every 409 path writes a `refused` line with `stage`; failures `failed`; log-write failure surfaces in `/api/fleet` as `log_error` |
| B9 | **Honest status text** | UX-03, GD-03 | 1 d | §7.5 acceptance |
| B10 | **Hub terms and network discipline** | HUB-01, HUB-02, HUB-03, UA wording | 0.5 d | §3.6 acceptance |
| B11 | **Panel polish** | §7.3 table, JS-01, poll cadence, library hashing | 2 d | JS + Playwright tests |
| B12 | **Expert UI decision** | UX-06, §7.7 | 0.5–1.5 d | one meaning of "Unit A" |
| B13 | **Generalisation** | config file, `SLOTS` from config, systemd templates, setup.sh single package | 2 d | §10.5 walk-through passes on a second Pi |
| B14 | **Refactor `web.py`; docstrings match code** | ARCH-01, §2.3 | 1 d | no behaviour change; suites green |
| B15 | **Fixture provenance** | §5.3: capture real m-flasher outputs and sysfs trees on the Pi; synthetic unit ids in tests | 0.5 d bench + 0.5 d | fixtures carry a "captured <date> on <tool version>" header |

Total ≈ 20–23 developer-days, plus bench time. B1–B3 are the P0 set (about 3 days). B4 is the
OSS-blocking feature.

### Phase C — post-publish

* **Hardware-tagged release checklist** (manual, since CI cannot reach units): one flash, one restore,
  one backup-now, one multi of 2, one bind, from the panel; photo of each modal; `fm1-flash.log`
  excerpt attached to the release.
* A self-hosted runner on the Pi, gated to tags, running TEST-04 against real units in a dedicated
  "sacrificial" slot. Optional; only if the owner wants it.
* Release process: version in `fm1_switch/__init__.py` and `pyproject.toml`; `CHANGELOG.md`; tag; a
  release note listing which features changed status from "fake" to "HW".
* Issue templates asking for `fm1-switch diagnose` output and `m-flasher --version`.

### Phase D — planned after the current development work

* **Firmwares that need their maker's installer** (Melodee, ChoralRoot): detection exists, but
  the panel cannot finish the install. Options and recommendation in
  [ROADMAP-installer-firmwares.md](ROADMAP-installer-firmwares.md).
* **Per-unit settings** (MIDI channel and similar) set from the fleet manager, starting with a
  read-only investigation of what the firmware accepts. See
  [ROADMAP-device-settings.md](ROADMAP-device-settings.md).
* **The Pi as a USB MIDI host and dawless hub** (routing, clock, channel remap, network MIDI,
  recording): starts with a read-only survey of what the units expose. See
  [ROADMAP-dawless-hub.md](ROADMAP-dawless-hub.md).

---

## 12. What was not verified

* **Anything on the Pi or the units.** Every "HW" status above is the handoff's or a commit's
  word **[documented]**. That covers flash, restore, auto-backup, restart, relearn, soft-key
  reach (contradictory records for unit C), timing, pruning on the Pi, and `--device` honouring
  in m-flasher.
* **Panel geometry on cog/WPE.** All layout numbers are Chromium at 800×480 with Mac fonts. The
  `vh`-based sizes should carry over. The modal's text-driven height may not. UX-01 needs one
  on-panel test, which is also its acceptance test.
* **Resistive press repetition into a newly opened modal.** Taken from the work order and the
  handoff (`fleet.html:36-37`), not measured.
* **Network behaviour.** No catalogue or package was fetched. `hub.py` network paths were read,
  not run. `compat_for` reaching the network was prevented by a monkeypatch in my probe server
  (`hub.load` forced to `network=False`), so HUB-01 is **[derived]**.
* **m-flasher's licence and exact output wording** (offline; not installed).
* **The sudoers grant** (not in the repo).
* **Whether `REAL_INFO_V13` is byte-exact to the tool's output.** Taken on the test's word.
* **CSRF end to end in a browser.** I measured that the server accepts a `text/plain`,
  foreign-Origin POST and acts on its JSON (a refusal path). I did not run a cross-origin page
  in a browser against a writable configuration.

*Audit artefacts:* the stub server and probe environment lived in the session scratchpad and are
not part of the repo. One file the probe created on this Mac (`/tmp/fm1-viewport.log`, 26 bytes,
from SEC-04's measurement) was deleted afterwards.
