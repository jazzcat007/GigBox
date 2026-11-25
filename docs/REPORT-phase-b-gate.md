# Phase B gate report — UX-01, SEC-01/02, SAFE-01

**Branch:** `phase-b/p0`, cut from `audit/oss-prep` at `cd36045`, ten commits on top.
**Date:** 2026-11-23. **Method:** no hardware, no network, no m-flasher (absent on this
Mac). Every number below is labelled: **[measured]** (ran it here and quote the result),
**[derived]** (read the code and reasoned; not executed), **[assumed]** (stated so it can
be checked against hardware), **[documented]** (a repo doc or commit says it).

```
47e014d  panel: a confirm dialog ignores clicks it did not earn (UX-01, part 1)
98c3e60  panel: the confirm dialog is placed away from the button that opened it (UX-01, part 2)
5e97b65  web: the API refuses requests that did not come from its own UI (SEC-01 gates)
9bdff0f  web: mutations from off-Pi clients need X-FM1-Token; the token is a 0600 file (SEC-01)
dde912c  web: bind to loopback by default; the LAN is an opt-in with a token (SEC-02)
b27c506  README: the status row reflects the UX-01 fix
f591a20  web: static serving is contained and allowlisted (SEC-03)
b7428ec  web: state files this process creates are owner-only (SEC-05)
8454e61  cli: remove the unguarded fm1-switch install (SAFE-01)
70dec0a  retire fleetctl; one package, one writer, one rule file (FC-01)
```

Every commit leaves the suites green **[measured, per commit below]**. One logical change
per commit; the README status-row refreshes that were forced by earlier commits are their
own commits so nothing is smuggled in.

---

## 1. Suite counts

| Suite | Before (baseline on `audit/oss-prep`) | After this phase | Status of the change |
|---|---|---|---|
| fm1-switch Python | **170 passed** [measured] | **207 passed** [measured] | +37: 33 request-gate/token/static/umask cases, 4 CLI cases |
| fm1-switch JS (`node --test tests/js/fleet.test.mjs`, node v24.22.2) | **34 passed** [measured] | **42 passed** [measured] | +8: 3 press/origin/arm, 4 geometry, 1 token header; harness gained `dispatch()`, a controllable clock, `getBoundingClientRect`, `localStorage` |
| fleetctl Python | **47 passed** [measured] | **0** — the package and its tests were removed with it [measured: `git rm -r fleetctl`, commit `70dec0a`] | retired by design |

No existing test was weakened or deleted. The two JS harness methods that changed
(`click()` now dispatches through the shared `dispatch(type, ev)`, and it keeps its old
`disabled` refusal) preserve the old semantics exactly; call sites that model a *human*
confirming now press the way a finger does (`pressGo`: past the arm window, `pointerdown`
on the button). Tests that assert a *blocked* click still use bare `click()`.

---

## 2. Item 1 — UX-01: a held press must never complete a write

All three fixes landed, as required — not a choice:

**(a) Opener-aware modal geometry** (`98c3e60`)
`fleet.js` measures the opener's bounding rect at confirm time (`openerCentre`,
`fleet.js:466-475`), and `actionPlan` (`:480-484`) places the actions row at the vertical
end of the box away from the opener (`cy >= vh/2` → actions at the top; all five of the
audit's openers qualify) and the confirm button at the horizontal end away from the
opener's centre-x (`go-left` when the opener sits in the right half). `fleet.css:255-257`
implements it (`display:flex; flex-direction:column` on `.modal-box`; `order: 2` moves the
body below the row; `flex-direction: row-reverse` flips the buttons). With no geometry at
all the pre-audit layout is kept and (b)+(c) carry the guarantee. The two measured
overlaps (SEVERAL `FLASH n UNITS` under WRITE: 13,366 px²; RESTORE `BACK UP` under its own
confirm: 6,630 px²) both go to zero by construction **[derived from the rule; the audit's
measured rects drive the new tests]**. The real-engine zero-overlap check (audit TEST-01)
still requires a browser run or the panel — owner checklist §6.1.

**(b) 600 ms arm** (`47e014d`)
`showConfirm` sets `state.armedAt = performance.now()` (`fleet.js:492`); `doWrite` returns
while `performance.now() - armedAt < ARM_MS` (`fleet.js:684-694`). The repeats of the
press that opened the dialog all land inside the window.

**(c) pointerdown-origin guard** (`47e014d`)
The dialog opens with `state.pressOnGo = false` (`fleet.js:493`); only a `pointerdown` on
the confirm button sets it (`fleet.js:895`); `doWrite` refuses any click whose own press
did not start there (`fleet.js:695`). A repeat click carries no new `pointerdown`, and a
synthetic click has none at all. Reset on close and on every open.

**Tests** (`fleet.test.mjs`): a click on WRITE with no press does nothing, even past the
window; a press inside the arm window does nothing and exactly one write happens just
past it; the five-repeat UX-01 scenario (200 ms steps out to 1000 ms, bare clicks) writes
nothing; the three audit-measured opener rects produce `actions-top` (+`go-left` on the
right-side openers); no-geometry fallback keeps today's layout and an armed pressed
confirm still writes; POSTs carry `X-FM1-Token` only when saved.

**Decisions worth review:**
* `Content-Type: application/json; charset=utf-8` is accepted for the server's 415 gate
  (tests say so). The gate's job is forcing a CORS preflight; any `application/json` body
  is non-simple with or without a charset parameter, so the preflight happens either way.
* Requests with **no Origin header** (curl, urllib, tests) pass the Origin gate; browsers
  cannot omit Origin on POST.
* Requests with **no Host header** pass the Host gate; a DNS-rebinding page has to
  present the attacker's name, so the gate still stops it, and browsers always send Host.
* The token is printed once at non-loopback startup to **stdout = the journal of the web
  unit, not the panel** (audit I-11's distinction), so the operator can hand it to the
  expert UI. The panel keeps displaying nothing (SETTINGS → ABOUT is audit B11/B4 work).
* `fm1-screen.service` / `console-setup` (retired console) are still tracked — audit §2.3
  recommends deleting them; that cleanup belongs to the systemd workstream (B13), not
  this gate, and nothing here re-enables them.

---

## 3. Item 2 — SEC-01/02: the API is not browser-attackable and is not on the LAN

Layered, in check order (`web.py` `Handler._gate`, `:661-692`), evaluated before the body
is read:

1. **Host allow-list → 421.** Loopback names (`127.0.0.1`, `localhost`, `::1`; `_host_name`
   strips ports and brackets, `:95-105`) plus `FM1_WEB_HOSTS` (comma-separated LAN names
   the operator configures deliberately; `_allowed_hosts`, `:87-97`). Closes the measured
   DNS-rebinding read (`Host: attacker.example` got 200 on `/api/fleet`).
2. **Origin check → 403.** When a browser sends Origin, its host must be in the same set
   (`urlparse(...).hostname`, so `null` origins fail).
3. **Content-Type → 415 on mutations.** Forces the CORS preflight; the server has no
   `do_OPTIONS` and never sends `Access-Control-Allow-*`, so the browser blocks it.
4. **Token on mutations → 401** (`9bdff0f`). Off-Pi clients must present
   `X-FM1-Token` (`_token_ok`, `:687-700`; `secrets.compare_digest`). Loopback — the
   panel — needs nothing. A LAN surface with **no** token configured fails closed.
   Generation: 6-digit code, written `O_CREAT|O_EXCL` mode **0600** to
   `FM1_TOKEN_FILE` (default `~/.config/fm1/token`), `FM1_TOKEN` env wins
   (`token_file/_load_token/_ensure_token`, `:112-152`).
5. **Loopback bind by default** (`dde912c`): `web.serve(host="127.0.0.1")` (`:1050`),
   `cmd_web` (`__main__.py:86`), `fm1-web.service` pins `--host 127.0.0.1` explicitly.
   The LAN stays reachable only by opt-in, and the same bind decision turns the token on.
6. **Static containment** (`f591a20`): `_static` requires `STATIC_DIR.resolve() in
   target.parents` (parents test, not string prefix — `/../web.py` had satisfied
   `.startswith()`) **and** the allowlist `STATIC_FILES` (`web.py:54-58`, `:1010-1019`), so
   source files 404 even in-tree.
7. **State files owner-only** (`b7428ec`): `serve()` calls `_harden_umask()` →
   `os.umask(0o077)` (`:1038-1056`); `fm1-web.service` gets `UMask=0077`. New flash log,
   registry saves, backups, token file: 0600/0700. **Pre-existing files keep their bits —
   one `chmod 600` pass is the owner's step (§6).**

`fleet.js` sends a stored token when one is saved (`savedToken/postJson`), `app.js` asks
once on a 401 and retries with it in `sessionStorage` (`api/tokenHeaders/askToken`).

**Out of scope, untouched as instructed:** the optional `sha256` on `/api/flash` remains
optional (audit SAFE-02 / roadmap item B6) — the audit log still records "the server's own
hash as if confirmed" in that case **[unchanged, verified by reading `web.py:292-304`]**.
No auth framework, no HTTPS, no new dependency at runtime (stdlib only on the Pi; nothing
added to `pyproject.toml`). `/api/viewport` keeps its GET-with-side-effect (SEC-04, P2) —
it is now loopback-only by the bind default and documented as accepted for the gate.

---

## 4. Item 3 — SAFE-01/FC-01: the port-addressed write paths do not exist

* `fm1-switch install` **removed** (`8454e61`): `cmd_install` deleted, USAGE/docstring
  rewritten in the same commit, `main()` has no `install` dispatch. The audit's guarded
  shape (CLI flash by id through a shared ops module) was deliberately *not* built — one
  writer means one confirmation model (the audit's own §7.7 reasoning). The panel and
  `/api/flash` remain the only writers, and both go through `guided.flash`.
* `fleetctl` **retired in one atomic commit** (`70dec0a`): `git rm -r fleetctl` (history
  keeps it; no rewrite, no push), udev rule moved verbatim to
  `fm1-switch/udev/80-fm1.rules` (`git mv`; two stale consumer mentions renamed, a
  provenance note added), `setup.sh` installs one package and the rule from its new home,
  `ci.yml` matrix drops fleetctl, README/CONTRIBUTING/docs-index updated, the two
  historical docs got banners. `docs/private/` and `archives/` (local-only by design)
  untouched **[verified: `git diff audit/oss-prep..HEAD --stat` shows no path under them]**.
  Untracked build leftovers (`fleetctl/.venv`, caches, egg-info) were removed from disk;
  the protected directories were not touched.
* What *did not* move: `registry.scan`'s udev-symlink dependency and `units.py`'s port
  map (audit ARCH-02) — that is workstream B5, explicitly out of scope here. The moved
  rule therefore still carries the port-symlink section, and the fleet view still needs a
  mapped bench until B5. Nothing that *writes by port* survives:
  `grep -rn "install_firmware" fm1-switch/fm1_switch` → only `mflasher.py` and the
  `guided.flash` call site **[measured]**.

**Tests:** `test_install_is_gone` (`tests/test_cli.py`) pins exit 2, the refusal message,
the absence of `fm1-switch install` from USAGE and the module docstring, and that
`mflasher.install_firmware` is unreachable from the CLI (monkeypatched to raise). No test
asserts fleetctl anymore because the package is gone; the 47-case suite was removed with
its unit **[measured]**.

---

## 5. Invariant spot-check — where each of the 11 is enforced now

All file:line verified on this branch HEAD `70dec0a`. "unchanged" = this phase did not
touch the enforcement site.

| # | Invariant | Enforced at (file:line) | Note after this phase |
|---|---|---|---|
| I-1 | Never guess which unit | `guided.py:51` (`looks_like_flash_id`), `guided.py:135-150` (id re-read), `registry.py:381` (`known`), `fleet.js:144` (`hasFlashId`) | unchanged; placeholders still refused server-side (`web.py` via `guided`) |
| I-2 | Two deliberate actions; second names the unit; a stationary press cannot complete it | `fleet.js:492-495` (arm + press-origin reset + opener-aware classes), `fleet.js:694-695` (the two guards in `doWrite`), `fleet.css:247-256` (placement), `web.py:381, 438, 470, 490, 967, 979` (`confirm == unit_id`) | **strengthened**; on-panel confirmation owed (§6.1). Server-side, `confirm` still proves the client copied the id — the SEC gates now bind the sender instead [derived] |
| I-3 | Only write in download mode | `guided.py:74-83,131,177-179` (`download_devices`, re-check), `guided.py:224` (restart path) | unchanged; the bypass routes (`fm1-switch install`, fleetctl) are gone |
| I-4 | Match by flash id or write nothing | `guided.py:116-119` (placeholder refusal), `:135-171` (no match ⇒ restart only) | unchanged; violates-by-construction paths removed with the CLI and fleetctl |
| I-5 | Nothing silently filtered | `fleet.js:313` (rank sort), `fleet.js:325` (marks) | unchanged (GET FIRMWARE "N more" copy is still audit P3) |
| I-6 | Back up first or write nothing | `guided.py:181-196` (`before_write` failure ⇒ refuse + restart), `web.py:499-505` (`_backup_before_write`), wired at `web.py:385, 447` | unchanged; backup contents still trusted on exit code (SAFE-04 = B6, open) |
| I-7 | One write at a time, refused not queued | `guided.py:120-122, 221-222` (non-blocking lock), `web.py:801` (fleet cache while busy) | now holds **process-wide within the product**: the only out-of-process writer (fleetctl) and the lockless CLI path are gone |
| I-8 | Server re-verifies confirm/sha/path | `web.py:292-304` (`_check_package`, sha optional — SAFE-02 open), `web.py:451-464` (`_backup_by_id_path` containment), `web.py:951` (package name check), **new:** `web.py:661-692` request gates, `:1014` static containment | strengthened |
| I-9 | `input="yes\n"`, "Nothing written" is failure | `mflasher.py:117` (single `subprocess.run`), `:140` (`_check_written` on every write path) | now true **for every writer in the repo** — fleetctl's `stdin=DEVNULL` path no longer exists |
| I-10 | Every request/outcome logged | `web.py:353` (request), `:363` (refused), `:372` (done), `:495, 973, 982` (delete/rename/restart) | unchanged; SAFE-05 (pre-guided refusals unlogged, mislabelled failures, swallowed log-write errors) is B8 and still open |
| I-11 | No debug output to a stdout that is the panel | `web.py:629-631` (`log_message` gated by `FM1_WEB_VERBOSE`), `web.py:1057-1064` (`serve` prints to the journal of the *web* unit), `fm1-web.service` (stdout=journal, not panel) | unchanged; token print lands in the journal deliberately. `fm1-screen.service`/`console-setup` still tracked (audit §2.3 recommends deleting them — B13, not done here) |

Nothing in this phase trades an invariant away. The two audit-flagged gaps that remain
open on **intended** roadmap items are SAFE-02/SAFE-04 (B6) and SAFE-05 (B8); the
measured SEC-01/02/03/05 holes and the SAFE-01/FC-01 paths are closed.

---

## 6. Owner hardware checklist (produce, do not run — no hardware was touched)

Everything below needs the Pi, the panel and the units. Nothing above claims any of it.

### 6.1 Stylus hold — the UX-01 acceptance test
1. Put **unit C** (or any) in download mode; select a firmware; open the unit page.
2. **Multi:** on the SEVERAL screen pick ≥2 units, then hold a stylus *stationary* on
   `FLASH n UNITS` for **3 s** with the dialog set to open under it.
   **Pass:** at release, no write has started (`/api/fleet` shows `busy: null`, nothing in
   `~/fm1-flash.log`, no units restarted), and the dialog sits on screen with its actions
   row at the **top** of the box.
3. While still holding, watch that a bare click on WRITE anywhere does nothing.
4. Then, with the stylus *lifted*: tap WRITE deliberately (finger or stylus). **Pass:** the
   write proceeds exactly as before (this proves the guard does not lock out humans).
5. Repeat step 2 on the unit page's FLASH (bottom-left opener) and RESTORE's BACK UP. On
   the unit page the actions row is top and WRITE stays on the **right**; on RESTORE it is
   top with WRITE at the **left**.
6. Also eyeball: no overlap between any opener and the confirm button at 800×480 (the
   Playwright geometry pass of audit TEST-01 would satisfy this in CI instead).

### 6.2 Kiosk-over-loopback check (the default path)
1. Confirm `fm1-web.service` runs with `--host 127.0.0.1`: `systemctl show fm1-web -p ExecStart`.
2. Power the Pi with no other server: the kiosk must load `/fleet`, show the fleet, and
   the SETTINGS → recent-activity row must appear (proves GETs over loopback pass the gates).
3. Do one harmless mutation from the panel **without any token**: rename a unit to its
   current name (or clear a name). **Pass:** no token prompt/error; `~/fm1-flash.log` gains
   the `rename` line, mode `-rw-------` (or 0600 on older files after the one-time chmod).
4. `su - $USER -c 'curl -s http://127.0.0.1:8080/api/fleet | head -c 200'` works;
   `curl http://<pi-lan-ip>:8080/api/fleet` from another machine **must fail** (connection
   refused) — that is the loopback default.

### 6.3 LAN opt-in check (only if you want the phone workflow)
1. Stop the user unit; run `FM1_WEB_HOSTS=<pi> .venv/bin/fm1-switch web --host 0.0.0.0
   --port 8080` in a terminal, where `<pi>` is the name or IP the phone will use — the
   Host gate answers any other name with 421 (`web.py` `_allowed_hosts`). **Pass:** it
   prints the 6-digit token and writes `~/.config/fm1/token` mode 0600
   (`ls -l ~/.config/fm1/token`).
2. From a phone on the bench Wi-Fi: open `http://<pi>:8080/` — pages render (GETs open).
3. From the phone, POST a mutation without the token (e.g. try renaming from the expert UI,
   cancel the token prompt): **expect** the 401 message naming `X-FM1-Token`.
4. Enter the token once, retry: **expect success**, and the token survives for the session
   only (close the tab, token gone).
5. From the phone, `curl -X POST http://<pi>:8080/api/library/remove -H 'Content-Type:
   text/plain' --data '{}'` → **415**; with a foreign `Host:` header → **421**; a page on
   another origin driving a POST → the browser preflights and fails (403 at worst).
6. One-time hygiene after this run: `chmod 600 ~/fm1-flash.log ~/fm1-units.json` (or walk
   `~/fm1-backups`) — pre-existing files keep their old bits by design of the umask fix.

### 6.4 Regression sanity (existing HW proofs, unchanged code paths)
One flash and one restore from the panel as in HANDOFF §13.2. The guided path is untouched
by this phase; this run only re-proves the web layer in front of it (gates + token + loopback
bind) and the panel UI (arm/press/geometry) against real cog/WPE timing. If the guard
misbehaves (e.g. a human tap inside 600 ms is refused — by design), say so in the report;
600 ms was chosen to be far below human tap-to-tap latency [assumed from the resistive
panel's repeat rate, which is documented but unmeasured here].

---

## 7. What this phase deliberately did not do

* Audit §11 items B4–B15 (bind/registration, sysfs identity, write-path integrity,
  concurrency, log honesty, status text, hub terms, polish, expert UI, generalisation,
  refactor, fixtures) — untouched.
* `docs/AUDIT-fleet-manager.md` — **byte-identical** to `audit/oss-prep`
  [measured: empty `git diff` for that path]. The report lives in this new file, not the audit.
* No history rewrite, no push, no deletion under `archives/`, `backups/`, `realtest/`,
  `docs/private/` [measured above].
* Runtime dependencies: still none (`fm1-switch/pyproject.toml` untouched); the JS suite
  still runs on Node built-ins alone.

*Residual unknowns for reviewers:* geometry numbers in §2(a) are rule-derived from the
audit's Chromium measurements; cog/WPE may differ — the owner checklist covers it. The
600 ms window and the token print-to-journal are design choices made here, both reversible
in one line (`ARM_MS`; delete the `print` in `serve()`).
