# Phase B/p1 report — registration from the panel (B4, FEAT-01) and the pre-publication scrub

**Branch:** `phase-b/p1`, cut from `phase-b/p0` at `e5bb7d2`. The block below lists the
deliverable commits and the report commit; two verification-corrections commits
followed while this report was being checked against HEAD, and `git log phase-b/p1`
is the definitive list. Local only (no remote exists; none was created —
**[measured:** `git remote -v` empty before and after**]**). **Date:** 2026-11-23.
**Method:** no hardware, no panel, no `m-flasher` on this machine. Every number below
is labelled **[measured]** (ran it here and quote the result), **[derived]** (read the
code and reasoned; not executed), **[documented]** (a repo doc, commit, or the audit
says it), or **[assumed]** (stated so it can be checked).

**The one-line safety statement, up front:** registration is metadata. Nothing in this
phase calls `guided.flash`, adds, shortens, or shortcuts any write path. The write
path — which re-verifies the flash id from the device at write time regardless of
bindings (`guided.py:135-150`, unchanged) — is byte-untouched by these commits
**[measured:** `git diff e5bb7d2..HEAD -- fm1_switch/guided.py` is empty**]**.

```
2d460d7  web: bind a unit from the panel — GET /api/register/scan, POST /api/register/bind (FEAT-01)
fc88492  panel: the REGISTER flow — settings + first-run entry, refusals with reasons, a two-step confirm
7e6203b  README: registering a unit is a panel flow, not a hand-edited JSON file
e75abf3  scrub: the bench's real flash ids become obviously fake fixtures
aae98da  scrub: placeholder identity in the systemd units, calibrate.py and the package metadata
7db7a2f  report: phase B/p1 (this file + docs/index.md)
ef60ab9  docs: the bind docstring states all eight refusals; the report's refusal map re-pinned
9a2a4de  docs: this report's header lists all eight commits by hash
(+ verification-corrections commits after these; git log phase-b/p1 is definitive)
```

---

## 1. Per-commit changes and suite results

Suite commands: `cd fm1-switch && .venv/bin/python -m pytest -q` and
`node --test tests/js/fleet.test.mjs` (node v24.x). Baseline at `e5bb7d2`: **207
Python passed, 42 JS passed [measured at the start of this phase]**.

| Commit | Change | Python | JS |
|---|---|---|---|
| `2d460d7` | The register machinery: `registration.scan_download_mode`, `registration.bind_from_scan`, `registration.free_letters` (`registration.py:253, 301, 358`), the two endpoints behind the existing gates (`web.py:735, 781`), the `bind` event in the flash log (`web.py:1011`, surfaced by `recent_flashes` at `:334`), the CLI's stale confirm hint fixed (`__main__.py`, `cmd_register`), and `tests/test_register_web.py` (20 cases). | **227** (+20) | 42 |
| `fc88492` | The panel: REGISTER view (`fleet.js:926-997`), first-run card on an empty fleet (`fleet.js:239-247`), SETTINGS entry (`:609`), bind rows in the activity list (`:598`), one CSS rule for the first-run card; 6 new JS tests. One existing test's matcher updated (`/No units seen yet/` → `/NO UNITS SEEN YET/`) because the copy moved to the card's uppercase house style — same assertion, same meaning; the change is called out in the commit message too. | 227 | **48** (+6) |
| `7e6203b` | README: the FEAT-01 status row moves from ❌ missing to 🧪 tested-with-fakes (no hardware row was claimed); install step 6 becomes the panel flow with its refusals and the write-path note. | 227 | 48 |
| `e75abf3` | Scrub 1: the four real flash ids → `aaaaaaaa00000001..04` (one consistent fake per unit across all eight files), the registry docstring's "measured across this fleet" claim corrected, the verbatim-capture fixture's provenance comment updated to say exactly which byte is no longer verbatim, and the LAN-shaped address in the security suite's loopback test → RFC 5737 `203.0.113.20`. | 227 | 48 |
| `aae98da` | Scrub 2: the three systemd units carry `/home/YOUR-USER` + `User/Group=YOUR-USER` under a one-line header comment; the two prose comments that named the user now say "that user"/"the service's user"; `tools/calibrate.py` derives the checkout from `Path.home()` with an `FM1_HOME` override; `pyproject.toml`'s author becomes a `YOUR-NAME` placeholder; the README's services step agrees. | 227 | 48 |
| `7db7a2f` | This report + the docs/index row. | measured at the commit (below) | measured |
| `ef60ab9` | `docs: the bind re-read's two scan-state refusals belong in the docstring (and this report's refusal map)`: the `bind_from_scan` docstring now names the empty-bus and two-or-more-unclaimed refusals its fresh read produces (a reviewer reading "the refusals, in the order they are checked" was getting six of the eight), and §2.2-§2.4/§4 above were re-pinned to exact line numbers at HEAD. Docstring and report only; behavior byte-identical. | measured at the commit (below) | measured |
| `9a2a4de` | `docs:` this header's commit list made complete (the two verification-corrections commits are named; `git log phase-b/p1` stays definitive). Report-only. | measured at the commit (below) | measured |

Every commit was measured green before it was committed, and re-verified afterwards by
checking out each commit detached and running both suites from scratch **[measured:
`e5bb7d2` 207/42; `2d460d7` 227/42; `fc88492`, `7e6203b`, `e75abf3`, `aae98da` all
227/48; HEAD 227/48 at the report commit and re-run at the correction commit]**. No
existing test was weakened or deleted; the counts only ever went up (207→227 Python,
42→48 JS).

---

## 2. Deliverable A — the register flow

### 2.1 Server surface (exactly two new endpoints, nothing else)

| Method | Path | Behind | Behavior |
|---|---|---|---|
| GET | `/api/register/scan` | Host + Origin gates (`do_GET` → `_gate(mutation=False)`) | One fresh look at the bus. **[derived]** Returns `{units[], unbound, free[], proposed, refusal}`; `units` is every download-mode unit — bound, unbound, readable, unreadable — each with the id **the server read in this scan**. `refusal` states, in words, why REGISTER cannot proceed right now (or is `null`); `proposed` is the letter the one unclaimed unit would get, or `null`. |
| POST | `/api/register/bind` | the full mutation gate stack: Host, Origin, `application/json`, token (`do_POST` → `_gate(mutation=True)`) | Body `{unit_id, confirm}`. Refuses with 409 (via `WriteRefused`) in a fixed order, then binds. See 2.2. |

Both touch devices only through reads (`m-flasher info`, one per download-mode
device — the same read `registry.scan` does) and write only `~/fm1-units.json`
atomically (through `registration.confirm` → `registry.Registry.bind` →
`registry.save`). No other endpoint, no state file, no writer **[measured: the new
code calls `guided.download_devices`, `mflasher.info`, `registry.*`, and nothing that
writes a unit]**.

### 2.2 The bind's rules, checked in order (all operator-facing messages)

`registration.bind_from_scan` (`registration.py:358-421`), each refusal an
`AttributionError` the handler maps to 409:

1. **The confirm names the id** (`confirm == unit_id`, `:388-389`) — the same
   `confirm == unit_id` pattern every deliberate act uses (`web.py`
   flash/restore/restart/rename sites). No binding without it.
2. **The id must look like a flash id** (`guided.looks_like_flash_id`, `:390-392`):
   `device:`, `port:`, `slot:` placeholders name nothing and are refused, exactly as
   the write path refuses them.
3. **No flash may be running.** `APP.lock` is taken non-blocking (`:393-394`); if a
   write holds it, REGISTER refuses ("another flash is already running; try again
   when it finishes") rather than queues or reads during a write — invariant 7's
   "refused not queued", applied to the new device-touching reads instead of joining
   the SAFE-03 race the audit already tracks for polls (B7).
4. **The same fresh read refuses the two impossible-or-ambiguous states** (`:397-403`):
   an empty bus ("no unit is in download mode — put the new unit into download mode
   and scan again"), and two or more unclaimed units — **with the count**, and never
   picking one (invariant 5).
5. **The id is read AGAIN, now, from the bus** (`:405-409`). A binding is minted only
   from an id **this call** has just read — never from the scan the view displayed (a
   unit can leave download mode between the two — the staleness refusal), never from
   a client-supplied id, a USB port, or an order. The client's `unit_id` field is
   only ever matched against the server's own fresh reads; if nothing on the bus
   reports it, the request is refused and the registry grows no binding.
6. **Already-registered ids are refused** (`:410-415`), with the letter named: "that
   unit is already registered as letter B — use rename to change its name". Moving a
   binding is not supported this phase (deliberately; §6).
7. **A free letter must exist** (`:416-418`). "Free" = no confirmed binding in it
   (§5 decision 3). The binding goes to the first free letter, re-derived at bind
   time; the letter is never client input. The binding itself is written by
   `registration.confirm` (`:418`) — the existing provisional→confirmed machinery.

### 2.3 The panel

* **Entry points:** SETTINGS → `REGISTER…` (`fleet.js:609`), and the first-run card on
  an empty fleet (`fleet.js:239-247`) — the audit's §7.3 empty-fleet dead end, fixed
  per spec §4.3. The summary line reads `SETUP` while nothing is known.
* **The view** (`fleet.js:948-985`) renders the scan honestly: every download-mode
  unit as a row — registered ones marked `REGISTERED AS B · BASS`, the single
  unclaimed one actionable, unreadable ones shown with their error — and the refusal,
  when there is one, as a row **and** in the footer. Nothing is filtered (invariant 5).
* **The confirm:** the shared `showConfirm`/`doWrite` machinery (`fleet.js:506`,
  `:712-750`), so the 600 ms arm window (`ARM_MS`, `:710`) and the press-origin guard
  (`pressOnGo`, `:1001`) cover REGISTER exactly as they cover WRITE. Its body names
  BOTH the full flash id and the letter, and states that it writes nothing to the
  unit. A held press on REGISTER cannot complete a binding it did not deliberately
  earn — pinned by a JS test that replays the five-repeat UX-01 pattern against the
  register dialog **[in the fake DOM; the real press test is the owner's, §7]**.
* **After:** the POST carries `{unit_id, confirm}` (`perform`, `fleet.js:657-662`); the
  success line uses the letter **from the response** (the server's binding, not the
  proposal shown earlier), and `doWrite` returns the view to the fleet with the scan
  cache dropped (`:743`). Refusals surface as `FAILED: <the server's words>`.

### 2.4 What the flow refuses — each pinned by a test

| Refusal | Where enforced | Pinned by |
|---|---|---|
| No unit in download mode | `registration.py:328-329` (scan), `:397-398` (bind) | `test_scan_with_no_unit_in_download_mode_says_so`, `test_bind_refuses_a_unit_that_left_download_mode_after_the_scan` |
| >1 unclaimed (with the count; never picking one) | `:330-332` (scan), `:400-403` (bind) | `test_scan_counts_two_unclaimed_units_and_refuses_with_the_count`, JS `every REGISTER refusal renders…` |
| Every download-mode unit already registered | `:333-334` (scan) | scan marks the bound rows |
| The unit already registered (names its letter; rename offered) | `:410-415` | `test_bind_refuses_a_unit_that_is_already_registered` |
| Every letter taken | `:337-338` (scan), `:416-417` (bind) (+ `free_letters`, `:253`) | `test_bind_refuses_when_every_letter_is_taken` |
| Staleness (unit left download mode after the scan) | `:405-409` | `test_bind_refuses_a_unit_that_left_download_mode_after_the_scan` |
| Confirm mismatch | `:388-389` | `test_bind_refuses_a_confirm_that_does_not_name_the_id` |
| Placeholder ids | `:390-392` | `test_bind_refuses_placeholder_ids` |
| Client-supplied id | `:405-409` (no fresh read matches) | `test_bind_never_trusts_a_client_supplied_id` |
| Write running | `:393-394` (bind), `:318-320` (scan) | `test_bind_refuses_while_a_write_holds_the_lock`, `test_scan_refuses_while_a_write_holds_the_lock` |
| Unreadable unit id | scan shows the error row (`_download_units`, `registration.py:284-297`); a lone unreadable unit says so (`:335-336`) | `test_an_unreadable_unit_is_shown_and_counts_as_unclaimed`, `test_a_single_unreadable_unit_cannot_be_registered` |
| The gates (415 / 401) | `web.py` `_gate`, inherited via `do_POST` | `test_bind_post_with_a_text_plain_body_is_refused_415`, `test_bind_post_from_off_pi_needs_the_token` |

### 2.5 The write path is untouched, and proven so

`git diff e5bb7d2..HEAD -- fm1_switch/guided.py fm1_switch/mflasher.py` → empty
**[measured]**. The pre-existing tests that prove the write path re-verifies ids at
write time (`test_guided.py`: placeholder refusal, port-shuffled id match, no
match ⇒ nothing written) all pass unmodified at every commit **[measured]**. The
README's new registration section keeps and restates that note.

---

## 3. Deliverable B — the scrub

### 3.1 What changed

* **Flash ids** (`e75abf3`): the bench's four real 16-hex unit ids (three sharing one
  long prefix, one with a distinct prefix) are replaced by `aaaaaaaa00000001..04`,
  one consistent fake per unit (A→01, B→02, C→03, D→04) across `registry.py`'s
  docstring and the seven fixture files (JS suite, `test_fm1_switch`,
  `test_guided`, `test_registration`, `test_registry_relearn`, `test_web_hub`,
  `test_web_security` — 62 real-id occurrences replaced: 58 in the fixtures plus the
  docstring's 4 **[measured]**; the eight files carry 70 fake-id occurrences at HEAD,
  the extra 8 being the 7 this phase's new JS tests had already written as fakes
  (`fc88492`; zero before it) and the 1 the new provenance comment in
  `test_fm1_switch` cites **[measured]**). Both format
  checks pass (`looks_like_flash_id` **[measured]**; the panel's `hasFlashId` regex
  **[measured]**). The docstring's "Measured across this fleet" became "The examples
  used throughout this repository's fixtures are obviously fake ids in that shape" —
  no false provenance. `REAL_INFO_V13` (documented as a verbatim hardware capture)
  keeps every byte except the id, and its comment now says exactly that.
* **A LAN-shaped string** (`e75abf3`): the security suite's loopback test used a
  `192.168.*.*` LAN literal as its "any non-loopback address" example — it matched
  the third sweep, so it is now `203.0.113.20` (RFC 5737 documentation range); the
  assertion is unchanged.
* **Identity** (`aae98da`): the three systemd units (`fm1-kiosk` 5 hits, `fm1-screen`
  8, `fm1-web` 4) now use `/home/YOUR-USER`, `User=YOUR-USER`, `Group=YOUR-USER`,
  under a one-line header comment; the two prose comments that carried the username
  were rewritten. `tools/calibrate.py` uses `Path.home()` (the README clones to
  `~/fm1`) with an `FM1_HOME` override — no username literal
  (`py_compile` clean **[measured]**). `pyproject.toml`'s author → `YOUR-NAME`
  placeholder (not in the spec's inventory, but the sweep demanded it; §5 decision 6).
  The README's services step now describes placeholders, so the two agree.
* **Not touched, by design:** the dated docs (`docs/*.md` records, the audit
  byte-identical to `audit/oss-prep` **[measured:** empty diff for that path**]**);
  `LICENSE` keeps the owner's real name deliberately; `docs/private/` was never read
  or written.

### 3.2 The sweep outputs, run at HEAD

The work order's three sweeps, run at the final HEAD (and at `aae98da` before the
report existed; the report adds only `docs/` files):

```
$ git grep -nE "<two real-id patterns: the 12-digit prefix shared by three units | the one distinct id>" HEAD
$ git grep -nIi "<the owner's first name>" HEAD -- ':!LICENSE'
$ git grep -nE "<the owner's handle | an @-domain | the 192.168-LAN pattern>" HEAD
```

Each returned no output and exit code 1 (no matches) **[measured]**. The commands are
described rather than transcribed on purpose: a tracked file that quotes the patterns
verbatim would itself match three sweeps, and the verifier should run the work
order's own commands rather than this report's paraphrase of them.

Remaining hex-like strings reviewed and deliberately left **[measured by sweep]**:
the fake convention `aaaaaaaa0000000N`; `ffffffffffffffff` (a pre-existing fake
"unbound" id in `test_registry_relearn.py:32`); the two EDID digests in
`test_make_edid.py` (md5 digests of generated test output, pinned per the audit
§5.3); sha256 test strings (`aaaa…`, `f`·64, `0`·64). IPv4 literals at HEAD are only
`127.0.0.1`, `0.0.0.0`, and the RFC 5737 documentation address `203.0.113.20`
this scrub itself introduced (§3.1) **[measured]**. The `.local` names in `test_web_security.py`
(`fm1.local`, `bench.local`, `a.local`, `b.local`) are generic test values for
`FM1_WEB_HOSTS` from the gate phase, not the bench's hostname **[derived from their
origin commit]** — left.

**One bench-specific thing noticed and NOT changed (report-only, per spec):** the
udev rule and `label-units.sh`/`setup.sh` carry this bench's measured hub topology —
`KERNELS=="1-1.1.2"` port paths (`fm1-switch/udev/80-fm1.rules:64-71`). These are
hardware facts, not personal identifiers; they deanonymize nothing, they are already
documented as "edit for your hub" (README §Install step 3), and inventing a different
topology would be a false claim. Generalising them belongs to B13, as the audit
plans. Flagged for the owner's awareness.

---

## 4. Invariants — only the rows this phase touched

The write-path invariants (I-3, I-4, I-6, I-9) are **not in this table because nothing
about them changed and nothing could change**: registration binds metadata only — it
calls no writer, adds no writer, shortcuts nothing (see the one-line statement at the
top). I-5 and I-10 were touched only in the sense of gaining additional honest
behavior; I-1/I-2/I-7 gained enforcement sites. Enforcement-site line numbers for the
untouched rows are in the gate report §5.

| # | Invariant | What this phase adds | Sites (this phase) |
|---|---|---|---|
| I-1 | Never guess which unit | The bind is minted only from an id the server read in download mode **during that request**; placeholders refused like the write path refuses them | `registration.py:390-392` (looks_like_flash_id), `:397-409` (fresh read/staleness); write path unchanged |
| I-2 | Two deliberate actions; second names the unit; held press cannot complete it | The bind POST's `confirm` must equal the flash id being bound; the dialog rides the shared 600 ms arm + press-origin guards and names the id AND the letter | `registration.py:388-389`; `fleet.js:710, 1001, 989-997`; JS pins the five-repeat scenario against REGISTER |
| I-5 | Nothing silently filtered | The scan renders every download-mode unit (unreadable ones keep an error row); a refusal is on-screen, stated, never a silent narrowing; an unreadable unit still counts as unclaimed | `registration.py:266-299`, `:321-341`; `fleet.js:948-985` |
| I-7 | One write at a time; refused, not queued | Both endpoints take `APP.lock` non-blocking and refuse while a write runs; they never read a unit mid-write and never queue | `registration.py:318-320` (scan), `:393-394` (bind) |
| I-10 | Every request/outcome logged | `bind` events land in `~/fm1-flash.log` and surface on SETTINGS | `web.py:1011`, `:334` |

No write-path invariant changed. Registration cannot become a write path without new
code that would have to re-earn every refusal above **[derived: the endpoints bind
through `registry.Registry.bind` and save the registry JSON only]**.

---

## 5. Design decisions worth review

1. **The scan endpoints find devices through `guided.download_devices()` (sysfs),
   not `registry.scan()`'s udev symlinks.** The register flow must work on a bench
   with no port map — that is its whole point (audit §6.2.1 prescribes exactly
   `download_devices()` + read, under the lock). `registry.scan()` itself is
   untouched; ARCH-02 (B5) stays open and owns it. Consequence: a REGISTER scan can
   succeed telling a truth the fleet view cannot show yet.
2. **Both endpoints refuse rather than queue while a flash runs** (the lock is taken
   non-blocking, `APP.lock`). Cheap, fail-safe, and consistent with
   `guided.restart_unit`. The existing poll endpoints keep their current behavior;
   fixing them is B7, not mine.
3. **A provisional binding does not hold its letter hostage.** `free_letters` counts
   only confirmed bindings, and the bind replaces a provisional one — this is literally
   the existing provisional→confirmed machinery (`registration.confirm` →
   `Registry.bind` clears the slot), so a CLI power-on registration can be finished
   from the panel with no new machinery. Pinning test:
   `test_bind_upgrades_a_provisional_binding_to_a_confirmed_one`.
4. **A device with an unreadable id counts as unclaimed**, so it triggers the
   >1-unclaimed refusal when relevant. Rationale: with two units in download mode you
   cannot see which box carries the readable id; an unreadable one contributes the
   same ambiguity while contributing nothing bindable, so say so and refuse. The cost
   is strictness; the write-refusal alternative was a silent narrowing, which is what
   invariant 5 forbids. Review welcome — this is a judgment call about a state the
   audit never enumerated.
5. **The >1-unclaimed refusal fires even though the confirm names an exact id.** The
   operator cannot map an id to a physical box, so ambiguity stays a refusal with the
   count and the remedy (spec §3.3 read literally).
6. **`pyproject.toml`'s author was replaced with a placeholder** although it was not in
   the spec's scrub inventory, because the spec's second sweep (the owner's first
   name, case-insensitively, everywhere but `LICENSE`) must be empty and only
   `LICENSE` is exempt. The placeholder keeps the metadata valid and obviously
   editable. The owner may restore their name at publish time; LICENSE already
   carries it.
7. **The register view re-derives nothing:** the server proposes the letter, names it
   in the dialog, and re-derives it at bind time; the success copy quotes the letter
   from the response. A stale dialog therefore cannot bind to a letter the server no
   longer considers free.
8. **`registration.confirm` keeps its name and signature**; the new
   `bind_from_scan` spells its payload field `confirm_id` so the parameter cannot
   shadow the module function (the first draft tripped over exactly that
   **[measured: TypeError in the first test run]**).

---

## 6. What this phase deliberately did not do

* **No power-on registration job** (`/api/register/start` etc., audit §6.2.2) — the
  CLI's power-on flow stays as it was; the panel can finish its provisional bindings
  (§5.3) but does not drive power-on watching.
* **No panel button that sends the download-mode soft key.** It has been measured
  as a broadcast (2026-11-21) and as pointed (every run since; 2026-11-23 live bench) (audit; `softkey.py` docstring
  **[documented]**), it would be a third endpoint beyond the spec's "GET scan + POST
  bind… Nothing else", and refusals already cover the messy states it would create.
  The README says plainly that no such button exists and how a unit reaches download
  mode today.
* **No letter choice, no unbind, no moving a binding** — refusal says "moving a
  binding is not possible from the panel yet" and the README notes it. A follow-up
  candidate for a later phase (small UI plus an `unbind` decision about history).
* **Out-of-scope items honored:** B5 (sysfs identity/ARCH-02), B6 (SAFE-02/04), B8
  (SAFE-05), B13 (`fm1-screen.service` stays tracked, just scrubbed), expert UI
  (B12), publication mechanics, any history operation, any push. `docs/AUDIT-fleet-manager.md` untouched.
* **No runtime dependency** (`pyproject.toml` `dependencies = []` unchanged); the JS
  suite still runs on Node built-ins. No build step introduced.
* No existing JS/Python test was weakened or deleted; the one matcher change is
  recorded in both the commit message and §1.

---

## 7. Owner hardware checklist addition (produce, not run — no hardware was touched)

After the gate checklist (§6 of the gate report), walk this **on-panel registration
run**. It is the acceptance test the §7 table of this phase's README row is waiting
for; nothing above claims any of it.

1. **Fresh state.** With the Pi running as usual, `mv ~/fm1-units.json
   ~/fm1-units.json.keep` (or use a bench unit that is not yet registered). Power the
   panel: the fleet should show the **SETUP** summary, the first-run card, and
   REGISTER A UNIT.
2. **One unit in download mode.** Put a single unregistered unit into download mode
   (with only that unit connected: `m-flasher download-mode`; or a unit left there by
   a failed boot). Tap REGISTER A UNIT (or SETTINGS → REGISTER…).
   **Pass:** the scan shows the unit with its real id tail and no sticker; the row
   reads `REGISTER AS STICKER <next free letter>`; the footer invites the confirm.
3. **The confirm names both.** Tap the row. **Pass:** the dialog shows the full
   16-hex id, the letter, and "Writes nothing to the unit".
4. **Held press (mirror of the gate report §6.1).** With the dialog open, hold a
   stylus on the REGISTER row for 3 s. **Pass:** no binding appears
   (`~/fm1-units.json` unchanged, no `bind` line in `~/fm1-flash.log`), the dialog
   stays up. A bare finger-tap on REGISTER anywhere does nothing. Then deliberately
   (press started on REGISTER, past the arm window): **Pass:** the fleet returns, the
   card carries the letter, `~/fm1-flash.log` gains `event=bind`, and the unit page
   offers NAME…/FLASH.
5. **Refusal walks, each stating its reason on the panel:**
   a. A second unit also in download mode → the scan/confirm refuses naming **2** and
      "leave only one connected"; restart one (RESTART UNIT on a registered unit's
      page) and rescan.
   b. Tap REGISTER on an already-registered unit's id (or re-open REGISTER after
      step 4) → refused naming its letter and suggesting rename.
   c. Let the unit leave download mode (power-cycle it) after the scan, then confirm →
      refused; the message says it left between scan and confirm; scan again.
   d. With all four letters confirmed-bound, scan a fifth unit → refused; every letter
      taken.
6. **The binding survives a flash — and stays subordinate to it.** Flash that unit
   from its page the normal way. **Pass:** the write completes as always, the
   relearn updates its fingerprint, and the card keeps its letter and id. Then,
   deliberately, `m-flasher info` the same unit and confirm the id matches the card's
   tail. Finally the negative check the safety model rests on: hand-edit
   `~/fm1-units.json` to bind the SAME id to a different letter and flash again —
   **Pass:** the write still lands on the unit whose flash reports that id (the
   registry's lie cannot redirect a write).
7. Restore `~/fm1-units.json.keep` if you used step 1.

---

## 8. Residual unknowns for reviewers

* Whether the 600 ms arm window and press-origin guard behave identically on
  cog/WPE (inherited from the gate phase; the owner checklist covers it).
* The first-run card's layout at exactly 800×480 in cog was never rendered here —
  the fake DOM has no geometry. The card reuses the existing `.unit` card style with
  one added rule (`fleet.css`, `.unit.first-run`); the on-panel look is part of the
  §7 walk.
* `m-flasher info` timing on a 3B+ while a REGISTER scan runs is unmeasured; the
  lock guarantees it cannot overlap a write, but a scan during another client's scan
  is serialized only by the lock, not by m-flasher itself **[derived]**.
* Whether any operator path actually NEEDS the power-on provisional flow once the
  panel flow exists (maybe the CLI flow becomes redundant) — an owner decision, noted
  for B13/B15.
