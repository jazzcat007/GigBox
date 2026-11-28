"use strict";

// The FM-1 fleet touch UI.
//
// Plain DOM and fetch, no framework: the Pi serves this from its own standard library,
// so nothing has to be installed or built to update the panel.
//
// Safety model, mirrored from the server:
//   * a write is a MODAL act -- it opens a dialog naming the unit and the firmware, and
//     the confirm button is placed away from (and armed against) the button that opened
//     it, so the same finger resting on the screen cannot complete it:
//       - the actions row sits at the vertical end of the box away from the opener,
//         and the confirm button at the horizontal end away from it (showConfirm);
//       - the dialog ignores clicks for the first ARM_MS after it opened;
//       - a click is only honoured when its own press STARTED on the confirm button.
//   * the server re-checks the package's sha256 and refuses if it changed;
//   * nothing is ever inferred about which unit is which. A unit with no recorded
//     sticker is shown by its id, never guessed at.

const state = {
  gen: 0,               // bumped by every render(); async renderers bail if it moved (JS-01)
  view: "fleet",        // "fleet" | "unit" | "settings" | "restore" | "multi" | "name" | "get" | "register"
  units: [],
  packages: [],
  selected: null,       // unit_id
  pkg: null,            // package name
  page: 0,
  pending: null,        // {unitId, pkgName, sha, label} awaiting confirmation
  notice: false,        // a write result is on screen; keep it until the user navigates
  busy: false,          // a write started from THIS panel is in progress
  serverBusy: false,    // the server says a write is running (any client)
  phase: "",            // what the running write is doing right now, as the server words it
  compat: new Map(),    // "chip|package" -> compat, so a redraw does not re-ask the server
  build: null,          // the UI build the page was loaded with; a change means reload
  lastEvent: null,      // the server's last write result, to explain a unit that did not come back
  serverNow: 0,         // the server's clock, to tell whether that result is recent
  armedAt: 0,           // performance.now() when the confirm dialog last opened
  pressOnGo: false,     // the press behind the current click started on the confirm button
  chooser: null,        // {u, b, opener, armedAt} while the RESTORE / DELETE chooser is open
  multi: null,          // {i, n, label} while several units are written one after another
  picked: new Set(),    // unit ids ticked on the "flash several" screen
  reveal: null,         // a firmware name to select and bring onto the visible page
  sig: "",             // what the screen was last drawn from (quiet polls skip identical redraws)
  rpage: 0,             // page of the backups list
  gpage: 0,             // page of the download list
  getlist: null,        // firmware the Pi could fetch, loaded when that screen opens
  router: null,         // /api/router: router status, saved patches, slots (the ROUTES screens)
  rpatch: null,         // the patch being looked at or edited (a name; may not be saved yet)
  draft: null,          // the route being made: {source, dest, remap, transpose, clock, cc}
  ppage: 0,             // page of the patch list
  rtpage: 0,            // page of one patch's routes
  sets: null,           // /api/sets: saved rig sets, library, slots, push status (the SETS screens)
  sname: null,          // the set being looked at (a name)
  spre: {},             // preflight per unit for that set: slot -> {blocking, warning}
  sticked: new Set(),   // units ticked for a push (a push may cover any subset of a set)
  spage: 0,             // page of the set list
  regscan: null,        // the REGISTER scan, fetched when that screen opens (never cached
                        // across opens: download mode is a state of the world, not of the page)
};

// Rows per page. The 5-inch panel cannot scroll, so a list is paged to what fits above the
// buttons; an upright phone has far more height (and can scroll), so it shows three times as many.
const portrait = () => typeof matchMedia === "function" && matchMedia("(orientation: portrait)").matches;
const rowsPer = (base) => (portrait() ? base * 3 : base);
const PAGE_BASE = 2;                             // firmware rows per page on a 480px panel
const $ = (sel) => document.querySelector(sel);

async function api(path, opts) {
  const res = await fetch(path, opts);
  const text = await res.text();
  let data;
  try { data = text ? JSON.parse(text) : {}; } catch { data = { error: text }; }
  if (!res.ok) throw new Error(data.error || `${res.status} ${res.statusText}`);
  return data;
}

function el(tag, attrs = {}, ...kids) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") n.className = v;
    else if (k === "text") n.textContent = v;
    else if (k === "disabled") { if (v) n.disabled = true; }
    else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
    else n.setAttribute(k, v);
  }
  for (const c of kids.flat()) {
    if (c === null || c === undefined || c === false) continue;
    n.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return n;
}

function status(text) { $("#status").textContent = text || ""; }

// ------------------------------------------------------------------ data

async function loadFleet(quiet = false) {
  const data = await api("/api/fleet");
  state.units = data.units || [];
  state.lastEvent = data.last_event || null;
  state.serverNow = data.now || 0;
  state.clock = data.clock || null;          // is the Pi's clock trusted? (no internet: it may be wrong)
  state.clockAt = Date.now();
  state.logError = data.log_error || "";   // the audit log could not be written (SAFE-05)
  state.serverBusy = adoptServerBusy(serverPhase(data));
  state.phase = serverPhase(data);
  // New UI code was deployed: reload so the panel never keeps running a stale page. Never
  // while a write is in flight or a dialog is open.
  if (data.build) {
    if (state.build && data.build !== state.build && !writing() && !state.pending
        && typeof location !== "undefined") { location.reload(); return; }
    state.build = data.build;
  }
  await loadLibrary();
  // A background poll that finds nothing new must not rebuild the screen: on a phone the
  // rebuild is visible as a flicker (and can swallow a tap). Compare what the screen is
  // drawn from; callers that just changed something (a write, a button) always redraw.
  const sig = JSON.stringify([state.units, state.lastEvent, state.logError, state.phase,
                              state.packages, clockSkewText()]);
  if (quiet && sig === state.sig) return;
  state.sig = sig;
  state.compat.clear();               // the library may have changed
  render();
}

async function loadLibrary() {
  const data = await api("/api/library");
  state.packages = data.items || [];
}

function unitById(id) { return state.units.find((u) => u.unit_id === id) || null; }

// ------------------------------------------------------------------ rendering

// The routes screens set their own footer hint (renderRoutes and friends).
const ROUTE_VIEWS = new Set(["routes", "patch", "clock", "routeadd", "routeopt", "pname", "ptype", "sets", "set"]);

function render() {
  state.gen++;
  const screen = $("#screen");
  screen.replaceChildren();
  // The fleet is a 2x2 grid of big cards; the unit and settings views are stacked
  // columns. One container, two layouts.
  screen.className = state.view === "fleet"
    ? "grid" + (state.units.length > 6 ? " cols4" : state.units.length > 4 ? " cols3" : "")
      + (needsMatching() ? " withbar" : "") : "";
  $("#back").hidden = state.view === "fleet";
  $("#back").disabled = writing();
  $("#settings").hidden = state.view === "settings";
  $("#routes").hidden = state.view !== "fleet";
  $("#sets").hidden = state.view !== "fleet";

  if (state.view === "fleet") renderFleet(screen);
  else if (state.view === "settings") renderSettings(screen);
  else if (state.view === "restore") renderRestore(screen);
  else if (state.view === "multi") renderMulti(screen);
  else if (state.view === "name") renderName(screen);
  else if (state.view === "type") renderType(screen);
  else if (state.view === "get") renderGet(screen);
  else if (state.view === "register") renderRegister(screen);
  else if (state.view === "routes") renderRoutes(screen);
  else if (state.view === "sets") renderSets(screen);
  else if (state.view === "set") renderSet(screen);
  else if (state.view === "patch") renderPatch(screen);
  else if (state.view === "clock") renderClock(screen);
  else if (state.view === "routeadd") renderRouteAdd(screen);
  else if (state.view === "routeopt") renderRouteOpt(screen);
  else if (state.view === "pname") renderPatchName(screen);
  else if (state.view === "ptype") renderPatchType(screen);
  else renderUnit(screen);

  const ready = state.units.filter((u) => u.download).length;
  const running = state.units.filter((u) => u.running).length;
  const off = state.units.filter((u) => !u.running && !u.download && !u.present).length;
  const unfinished = state.units.filter((u) => u.setup_pending).length;
  $("#summary").textContent = !state.units.length ? "SETUP"
    : [running ? `${running} RUNNING` : "", ready ? `${ready} READY` : "",
       unfinished ? `${unfinished} UNFINISHED` : "", off ? `${off} OFF` : ""]
        .filter(Boolean).join(" · ") || "NONE";
  // Say what to do next. The footer is the only place that can carry an instruction, and
  // an empty footer looks like a broken screen rather than a calm one.
  if (writing() && !state.pending) {
    status(progressText());
  } else if (!state.pending && !state.notice && !ROUTE_VIEWS.has(state.view)) {
    status(state.view === "get" ? getStatusText()
         : !state.units.length ? "PUT A UNIT IN DOWNLOAD MODE, THEN REGISTER IT"
         : state.view === "fleet" ? "TAP A UNIT TO MANAGE IT"
         : state.view === "settings" ? "FIRMWARE IS MANAGED ON THE WEB ADMIN"

         : state.view === "unit" && attention(unitById(state.selected) || {}) ? attention(unitById(state.selected))
         : state.pkg ? `SELECTED: ${state.pkg.replace(/\.fwsc$/, "")} — TAP FLASH`
         : "TAP A FIRMWARE, THEN FLASH");
  }
}

// A flash id is hex. "port:..." / "device:..." are placeholders for a unit whose id cannot
// be read, and nothing may be written to those: the server refuses too.
function hasFlashId(u) { return /^[0-9a-fA-F]{8,}$/.test(u.unit_id || ""); }

// "Unit A", or "Unit A (BASS)" once the operator has named it.
// A sticker that is a guess (a power-on fingerprint, or a last-seen port that broke a tie
// between look-alike units) is drawn "A~" so it is never mistaken for a proven one.
function letterOf(u) { return (u.label || "?") + (u.label && (u.provisional || u.hint) ? "~" : ""); }

function title(u) { return `Unit ${u.label}` + (u.name ? ` (${u.name})` : ""); }

// Why a unit that should be there is not: the last write named it (or left it stuck) and it
// has not come back since. Empty when there is nothing to say. A unit that IS running or sits
// in download mode needs no explanation: the screen already offers RESTART UNIT for the latter.
const RECENT_S = 900;
function attention(u) {
  const ev = state.lastEvent;
  if (!ev || !u.unit_id || u.running || u.download) return "";
  if (state.serverNow && ev.at && state.serverNow - ev.at > RECENT_S) return "";
  const noRestart = "DIDN'T RESTART: POWER-CYCLE IT, OR RESTORE…";
  if ((ev.stuck || []).includes(u.unit_id)) return noRestart;
  if (ev.unit_id !== u.unit_id) return "";
  if (!ev.ok) return "LAST WRITE FAILED: " + String(ev.message || "").slice(0, 60);
  if (ev.relearned === "not-saved") return "WRITTEN AND RUNNING, BUT NOT RE-LEARNED: RESTART THE PANEL";
  return ["ok", "unbound", "shared", undefined].includes(ev.relearned) ? "" : noRestart;
}

// What the main button says and whether it may be pressed.
//
// `busy` is this panel's OWN write. `serverBusy` is another client's write, discovered by
// asking the server. They are deliberately separate: the server reports its busy state as
// `null` when idle and as a progress STRING while writing, and the client must not mistake
// its own acknowledged write for someone else's.
function writing() { return state.busy || state.serverBusy; }

// The footer while a write runs: the server's current step, so a minute-long wait is not silent.
function progressText() {
  const step = (state.phase || "Starting…").replace(/…$/, "");
  const m = state.multi ? `UNIT ${state.multi.label} (${state.multi.i}/${state.multi.n}): ` : "";
  return `${m}${step.toUpperCase()}… DO NOT UNPLUG`;
}

// The server's busy STRING, or "" when it is idle. `busy` is null or a string, never a bool.
function serverPhase(d) { return typeof d.busy === "string" ? d.busy : ""; }

// Should this panel lock itself because the SERVER reports a busy write? The server clears
// its busy string before it answers a flash request, so a non-empty string on a fresh
// response means a write is really running (this panel's own, or another client's).
function adoptServerBusy(phase) { return !!phase; }

// The grey button says what the write is doing right now, not just that it is busy.
function busyLabel() {
  const step = (state.phase || "").replace(/…$/, "");
  return step && step !== "Starting" ? `FLASHING: ${step.toUpperCase()}…` : "FLASHING…";
}

// Refresh just the step text (not the whole screen) while a write runs.
async function pollPhase() {
  try {
    const data = await api("/api/fleet");
    const phase = serverPhase(data);
    const changed = phase !== state.phase;
    state.phase = phase;
    // Release the lock as soon as the server says it is idle. Without this a panel that lost
    // the race at the end of a write would stay locked until the page was reloaded.
    if (!phase) state.serverBusy = false;
    if (state.busy) { status(progressText()); if (changed && phase) render(); }
    else if (!state.serverBusy) await loadFleet(true);
  } catch (err) { if (!state.busy) throw err; /* a write continues; the next tick tries again */ }
}

function flashButton(u, hasPkg) {
  if (writing()) return { text: busyLabel(), ok: false, busy: true };
  if (!hasFlashId(u)) return { text: "NOT REGISTERED", ok: false };
  if (u.download) return { text: "FLASH", ok: hasPkg };
  if (u.running) return { text: "FLASH", ok: hasPkg };
  return { text: u.present ? "NOT READY" : "NOT CONNECTED", ok: false };
}

function stateText(u) {
  if (attention(u).startsWith("DIDN'T")) return "DIDN'T RESTART";
  // A two-step firmware is not finished until the operator confirms the maker's installer.
  if (u.setup_pending) return "SETUP UNFINISHED";
  if (u.download) return "READY TO FLASH";
  if (u.running) return "RUNNING";
  if (u.present) return "SETTING UP";
  return "NOT CONNECTED";
}

function stateClass(u) {
  if (attention(u) || u.setup_pending) return "attn";   // not a unit that is ready
  return u.download ? "ready" : u.present || u.running ? "" : "absent";
}

// The Pi has no battery clock. With no internet its date can be wrong, and a phone knows the right time.
// Only when the Pi says its clock is untrusted AND this phone disagrees by more than two minutes.
function clockSkewSeconds() {
  const c = state.clock;
  if (!c || c.trusted || !c.now) return 0;
  const serverNow = c.now + (Date.now() - (state.clockAt || Date.now())) / 1000;
  const skew = Date.now() / 1000 - serverNow;
  return Math.abs(skew) > 120 ? skew : 0;
}

function clockSkewText() {
  const s = Math.abs(clockSkewSeconds());
  if (!s) return "";
  if (s < 5400) return `${Math.round(s / 60)} MIN`;
  if (s < 172800) return `${Math.round(s / 3600)} HOURS`;
  return `${Math.round(s / 86400)} DAYS`;
}

function requestClockSet(opener) {
  showConfirm({ action: "clock-set", unitId: "clock", label: "Clock" },
    "SET THE CLOCK FROM THIS PHONE?", "SET CLOCK", opener,
    el("div", {}, el("b", { text: `The Pi's clock is off by about ${clockSkewText()}` })),
    el("div", { text: "It has no internet to set it. This phone's time is used. The audit log and backup names carry the date." }),
    el("div", { class: "warn", text: "Not while a unit is being flashed." }));
}

function renderFleet(screen) {
  if (state.logError) screen.append(el("div", { class: "fwrow", text: "WARNING: " + state.logError.toUpperCase() }));
  if (clockSkewText()) {
    screen.append(el("div", { class: "regbar" },
      el("span", { class: "state attn", text: `PI CLOCK OFF BY ${clockSkewText()}` }),
      el("button", { class: "primary", text: "SET FROM THIS PHONE", disabled: writing(),
                     onclick: (ev) => requestClockSet(ev) })));
  }
  if (!state.units.length) {
    // First run (audit §7.3: the empty fleet was a dead end that pointed at a SETTINGS
    // screen which could not help). One card that says what to do, with the REGISTER
    // action on it; the register screen does the rest.
    screen.append(el("div", { class: "unit first-run" },
      el("span", { class: "letter", text: "?" }),
      el("span", { class: "state", text: "NO UNITS SEEN YET" }),
      el("span", { class: "fw" },
        "Put a new unit into download mode. The Pi reads its flash id — the one " +
        "identity a unit has — and gives it a letter. Nothing is written to the unit."),
      el("span", { class: "meta" },
        el("button", { class: "primary", text: "REGISTER A UNIT", onclick: () => openRegister() })),
    ));
    return;
  }
  if (needsMatching()) {
    // A bar ABOVE the unit grid (not a grid cell: the grid clips, and a card squeezed into
    // one of its rows lost its button on the panel, 2026-11-27).
    screen.append(el("div", { class: "regbar" },
      el("span", { class: "state attn", text: "FLEET NOT REGISTERED" }),
      el("button", { class: "primary", text: "RE-REGISTER FLEET", disabled: writing(),
                     onclick: (ev) => requestReconcile(ev) })));
  }
  for (const u of state.units) {
    const chip = u.chip_kind
      ? el("span", { class: u.chip_kind === "new" ? "chip-new" : "", text: `${u.chip_kind} chip` })
      : el("span", { text: "chip ?" });
    // The last four of the flash id: three of the four units share a long prefix, so the
    // tail is what actually distinguishes them at a glance.
    const tail = u.unit_id && !u.unit_id.startsWith("port:") && !u.unit_id.startsWith("device:")
      ? u.unit_id.slice(-4) : "";
    screen.append(el("button", {
      class: "unit",
      onclick: () => openUnit(u.unit_id),
    },
      el("span", { class: "letter", text: letterOf(u) }),
      el("span", { class: `state ${stateClass(u)}`, text: stateText(u) }),
      el("span", { class: "fw", text: (u.name ? u.name + (u.firmware ? " · " + u.firmware : "")
                          : u.firmware || (u.running ? "running" : "—")) }),
      el("span", { class: "meta" },
        u.update ? el("span", { class: "chip-new", text: `UPDATE ${u.update.latest}` }) : chip,
        tail ? el("span", { text: "id " + tail }) : null,
      ),
    ));
  }
}

// After a reconnect the registered letters can show NOT CONNECTED while the same units show
// as "?": running units that look alike cannot be told apart from USB, only by flash id.
// Both signs together mean "match them again"; a "?" with no missing letter is a new unit.
function needsMatching() {
  const strays = state.units.some((u) => u.running && !u.slot);
  const lost = state.units.some((u) => u.slot && !u.running && !u.download);
  // A letter chosen only by a last-seen port (hint "port") may have swapped with a look-alike.
  return (strays && lost) || state.units.some((u) => u.running && u.slot && u.hint === "port");
}

function requestReconcile(opener) {
  showConfirm({ action: "reconcile", unitId: "reconcile", label: "Fleet" },
    "RE-REGISTER THE FLEET?", "RE-REGISTER", opener,
    el("div", {}, el("b", { text: "Match every plugged-in unit to its profile (A–D)" })),
    el("div", { text: "Every running unit goes into download mode, the Pi reads each one's flash id and " +
                      "matches it to its profile, then restarts them all. Nothing is written to a unit." }),
    el("div", { class: "warn", text: "Sound stops for about half a minute." }));
}

function openUnit(unitId) {
  state.notice = false;
  state.selected = unitId;
  state.pkg = null;
  state.page = 0;
  state.view = "unit";
  status("Tap a firmware, then FLASH.");
  render();
}

// Compatibility of one package with one unit's chip, asked once and remembered.
async function compatFor(u, name) {
  const key = `${u.chip || ""}|${name}`;
  if (!state.compat.has(key)) {
    let compat = null;
    try {
      const chip = u.chip ? `&chip=${encodeURIComponent(u.chip)}` : "";
      compat = (await api(`/api/fleet/package?name=${encodeURIComponent(name)}${chip}`)).compat;
    } catch { /* leave unknown */ }
    state.compat.set(key, compat);
  }
  return state.compat.get(key);
}

async function renderUnit(screen) {
  const gen = state.gen;
  const u = unitById(state.selected);
  if (!u) { state.view = "fleet"; render(); return; }

  screen.append(el("div", { class: "detail-head" },
    el("span", { class: "letter", text: letterOf(u) }),
    el("div", { class: "facts" },
      el("div", {}, el("b", { text: stateText(u) }), u.name ? ` · ${u.name}` : "",
        u.firmware ? ` · ${u.firmware}` : ""),
      el("div", {}, u.chip ? `${u.chip_kind || ""} chip (${u.chip})` : "chip unknown"),
    ),
    u.update && !writing()
      ? el("button", { class: "foot-btn update", text: `UPDATE ${u.update.latest}`, onclick: () => takeUpdate(u) })
      : null,
    hasFlashId(u) && !writing()
      ? el("button", { class: "foot-btn", text: "NAME…", onclick: () => { state.view = "name"; render(); } })
      : null,
  ));
  const why = attention(u);
  if (why) screen.append(el("div", { class: "fw", style: "font-size:20px;color:var(--orange)",
                                     text: why + "  Switch it off and on again." }));
  if (u.setup_pending) {
    const pkg = String(u.setup_pending.package || "").replace(/\.fwsc$/, "");
    screen.append(el("div", { class: "fw", style: "font-size:20px;color:var(--orange)",
      text: u.setup_from_pi
        ? `SETUP UNFINISHED: ${pkg} needs its maker's installer step. Press INSTALL FROM THE PI.`
        : `SETUP UNFINISHED: ${pkg} needs its maker's installer step. Finish it on a laptop ` +
          "(Chrome or Edge, USB), then press FINISH SETUP here." }));
  }

  // Compatibility is judged by the server, per package, against THIS unit's chip.
  const chips = u.chip ? `?chip=${encodeURIComponent(u.chip)}` : "";
  // Cached per chip+package so redrawing (page turn, selection, busy) is instant; the
  // first draw asks for all packages at once rather than one after another.
  const rows = await Promise.all(state.packages.map(async (p) => ({ p, compat: await compatFor(u, p.name) })));
  if (gen !== state.gen) return;          // a newer render owns the screen
  // Compatible first, then unknown, then chip-unaware. Nothing is hidden: on an old-chip
  // unit every firmware works, and on a new-chip one an old firmware still boots -- it
  // just will not keep saves, which is a warning, not a prohibition.
  const rank = { ok: 0, unknown: 1, warn: 2 };
  rows.sort((a, b) => (rank[a.compat?.severity ?? "unknown"] - rank[b.compat?.severity ?? "unknown"])
                      || a.p.name.localeCompare(b.p.name));

  // An update (or a fresh download) selects its firmware AND shows it: only a visible row may
  // be flashed, so the page is turned to it rather than selecting something off screen.
  if (state.reveal) {
    const at = rows.findIndex((r) => r.p.name === state.reveal);
    if (at >= 0) { state.page = Math.floor(at / rowsPer(PAGE_BASE)); state.pkg = state.reveal; }
    state.reveal = null;
  }
  const pages = Math.max(1, Math.ceil(rows.length / rowsPer(PAGE_BASE)));
  state.page = Math.max(0, Math.min(state.page, pages - 1));
  const chunk = rows.slice(state.page * rowsPer(PAGE_BASE), (state.page + 1) * rowsPer(PAGE_BASE));

  const list = el("div", { class: "list" });
  for (const { p, compat } of chunk) {
    const sev = compat?.severity ?? "unknown";
    // Words, not symbols: readable at arm's length, and "x" no longer reads as "forbidden".
    const mark = p.severity !== "ok" ? "NEEDS INSTALLER" : sev === "warn" ? "CHECK NOTE" : sev === "unknown" ? "NOT CHECKED" : "";
    const markCls = sev === "warn" ? "warn" : "unknown";
    list.append(el("button", {
      class: "fwrow" + (state.pkg === p.name ? " selected" : ""),
      disabled: writing(),
      onclick: () => selectFirmware(p.name),
    },
      el("span", { text: p.name.replace(/\.fwsc$/, "") }),
      mark ? el("span", { class: `mark ${markCls}`, text: mark }) : null,
    ));
  }
  if (!state.packages.length) {
    list.append(el("div", { class: "fwrow",
      text: "NO FIRMWARE ON THIS PI. SETTINGS → GET FIRMWARE" }));
  }
  screen.append(list);

  if (pages > 1) {
    screen.append(el("div", { class: "pager" },
      el("button", { text: "PREV", disabled: state.page === 0 || writing(),
                     onclick: () => changePage(-1) }),
      el("span", { class: "count", text: `${state.page + 1} / ${pages}` }),
      el("button", { text: "NEXT", disabled: state.page >= pages - 1 || writing(),
                     onclick: () => changePage(+1) }),
    ));
  }

  const sel = rows.find((r) => r.p.name === state.pkg);
  const note = sel?.compat?.note;
  if (note && sel.compat.severity !== "ok") {
    screen.append(el("div", {
      class: "fw", style: "font-size:20px;color:var(--orange)",
      text: note,
    }));
  }

  const fb = flashButton(u, !!state.pkg);
  screen.append(el("div", { class: "actions" },
    el("button", {
      class: "primary" + (fb.busy ? " busy" : ""),
      disabled: !fb.ok,
      text: fb.text,
      onclick: (ev) => requestFlash(u, sel, ev),
    }),
    // A unit left in download mode (a failed restart, or one you put there) can be sent
    // back to its running firmware without writing anything.
    u.download && hasFlashId(u) && !writing()
      ? el("button", { class: "ghost", text: "RESTART UNIT", onclick: () => restartUnit(u) })
      : null,
    state.pkg && hasFlashId(u) && !writing() && flashable().length > 1
      ? el("button", { class: "ghost", text: "SEVERAL…", onclick: () => openMulti(u) })
      : null,
    u.setup_pending && u.setup_from_pi && hasFlashId(u) && !writing()
      ? el("button", { class: "ghost", text: "INSTALL FROM THE PI…", onclick: (ev) => requestPiInstall(u, ev) })
      : null,
    u.setup_pending && hasFlashId(u) && !writing()
      ? el("button", { class: "ghost", text: "FINISH SETUP…", onclick: (ev) => requestFinish(u, ev) })
      : null,
    hasFlashId(u) && !writing()
      ? el("button", { class: "ghost", text: "RESTORE…",
                       onclick: () => { state.rpage = 0; state.view = "restore"; render(); } })
      : null,
  ));
}

// Pick up an update: select the newer firmware, fetching it from the hub first if the Pi
// does not hold it yet.
async function takeUpdate(u) {
  const name = u.update.package;
  if (!name) return;
  if (!u.update.have) {
    state.busy = true;
    state.phase = "Downloading…";
    render();
    status(`DOWNLOADING ${u.update.name.toUpperCase()}…`);
    try {
      await api("/api/download", { method: "POST", headers: { "Content-Type": "application/json" },
                                   body: JSON.stringify({ id: u.update.id }) });
    } catch (err) {
      state.busy = false; state.phase = "";
      render();
      status(`DOWNLOAD FAILED: ${err.message}`);
      return;
    }
    state.busy = false; state.phase = "";
    await loadLibrary();
  }
  state.reveal = name;
  render();
}

// A unit's saved whole-flash images. Restoring replaces EVERYTHING on the unit (firmware and
// its presets/sequences) with the state at that save, so it is a separate screen from the
// firmware list and its confirmation says so. Also where a backup is taken by hand or deleted.
const RPAGE_BASE = 3;  // backup rows per page: the panel is 480px tall
async function renderRestore(screen) {
  const gen = state.gen;
  const u = unitById(state.selected);
  if (!u) { state.view = "fleet"; render(); return; }
  let items = [];
  let space = null;
  try {
    const r = await api(`/api/backups?unit_id=${encodeURIComponent(u.unit_id)}`);
    items = r.items || [];
    space = r.space || null;
  } catch { /* shown as empty */ }
  if (gen !== state.gen) return;
  const pages = Math.max(1, Math.ceil(items.length / rowsPer(RPAGE_BASE)));
  state.rpage = Math.max(0, Math.min(state.rpage, pages - 1));
  const list = el("div", { class: "list" });
  if (!items.length) list.append(el("div", { class: "fwrow", text: "NO BACKUPS FOR THIS UNIT" }));
  for (const b of items.slice(state.rpage * rowsPer(RPAGE_BASE), (state.rpage + 1) * rowsPer(RPAGE_BASE))) {
    const when = `${b.saved.slice(0, 4)}-${b.saved.slice(4, 6)}-${b.saved.slice(6, 8)} ${b.saved.slice(9, 11)}:${b.saved.slice(11, 13)}`;
    const mine = !b.id.startsWith("auto/");          // taken by hand, never pruned
    list.append(el("div", { class: "pair" },
      el("button", {
        class: "fwrow", disabled: writing(),
        text: `${mine ? "★ " : ""}${when}  ${b.firmware || b.id}`,
        onclick: (ev) => requestRestore(u, b, ev),
      }),
      el("button", { class: "fwrow del", text: "DEL", disabled: writing(),
                     onclick: (ev) => openChooser(u, b, ev) }),
    ));
  }
  screen.append(list);
  screen.append(el("div", { class: "actions" },
    el("button", { class: "ghost", text: "BACK", onclick: () => { state.view = "unit"; render(); } }),
    pages > 1 ? el("button", { class: "ghost", text: `‹ ${state.rpage + 1}/${pages}`, disabled: state.rpage === 0,
                               onclick: () => { state.rpage--; render(); } }) : null,
    pages > 1 ? el("button", { class: "ghost", text: "›", disabled: state.rpage >= pages - 1,
                               onclick: () => { state.rpage++; render(); } }) : null,
    el("button", { class: "primary", text: "BACK UP", disabled: writing(),
                   onclick: (ev) => requestBackup(u, ev) }),
  ));
  if (!state.pending && !state.notice && !writing()) {
    const low = space && space.free_mb !== null && space.free_mb < 200;
    const room = space && space.free_mb !== null
      ? `  ${low ? "LOW DISK: " : ""}${space.free_mb >= 1024 ? (space.free_mb / 1024).toFixed(1) + " GB" : space.free_mb + " MB"} FREE`
      : "";
    status(`${title(u).toUpperCase()}: ${items.length} BACKUP${items.length === 1 ? "" : "S"} (★ = YOURS)${room}`);
  }
}

// Where the confirm actions belong, measured against the button that opened the dialog
// (audit UX-01 fix (a), paired with the arm window and the press-origin guard):
//   * vertical: the actions row sits at the END of the box away from the opener. Every
//     opener today is in the bottom half of the panel, so the row goes to the top and
//     the measured overlaps (SEVERAL 13,366 px^2, BACK UP 6,630 px^2) become zero.
//   * horizontal: the confirm button takes the end its opener's centre is furthest
//     from, so the row's far button is never under the opener either.
// No geometry at all (only in tests or a broken engine) keeps the pre-audit layout; the
// other two guards then carry the guarantee alone.
function openerCentre(ev) {
  const n = ev && ev.currentTarget;
  if (n && typeof n.getBoundingClientRect === "function") {
    const r = n.getBoundingClientRect();
    return { cx: r.left + r.width / 2, cy: r.top + r.height / 2 };
  }
  return { cx: null, cy: null };
}

function actionPlan(cx, cy, vw, vh) {
  if (cx === null || cy === null) return { top: false, goLeft: false };
  return { top: cy >= vh / 2, goLeft: cx > vw / 2 };
}

// One confirmation box for every act that changes the Pi or a unit. `go` is the red button.
// Arming: a resistive panel repeats a held press, and what sat under the finger when the
// dialog opened may be a confirm button now. So every new dialog is unarmed for ARM_MS,
// the press-origin guard refuses any click whose own press did not start on the confirm
// button, and the actions row is re-placed away from the opener. All reset per open.
function showConfirm(pending, heading, go, opener, ...body) {
  state.pending = pending;
  state.armedAt = performance.now();
  state.pressOnGo = false;
  const { cx, cy } = openerCentre(opener);
  const plan = actionPlan(cx, cy, window.innerWidth, window.innerHeight);
  $("#confirm-box").className = plan.top ? "modal-box actions-top" : "modal-box";
  $("#confirm-actions").className = "modal-actions" + (plan.goLeft ? " go-left" : "");
  const t = $("#confirm-title");
  if (t) t.textContent = heading;
  $("#confirm-go").textContent = go;
  $("#confirm-body").replaceChildren(...body.filter(Boolean));
  $("#confirm").hidden = false;
  status(`Confirm with ${go}, or CANCEL.`);
}

const PUT_IN_DOWNLOAD = "The unit is put into download mode first. Other units may restart for a few seconds.";
const dim = (text) => el("div", { style: "margin-top:6px;color:var(--dim);font-size:20px", text });

function requestRestore(u, b, opener) {
  showConfirm({ action: "restore", unitId: u.unit_id, label: title(u), backup: b.id, pkgName: b.firmware || b.id },
    "RESTORE THIS UNIT?", "WRITE", opener,
    el("div", {}, el("b", { text: title(u) }), u.firmware ? ` · currently ${u.firmware}` : ""),
    el("div", {}, el("b", { text: `RESTORE ${b.firmware || b.id}` }), ` · saved ${b.saved}`),
    el("div", { class: "warn", text: "Replaces EVERYTHING on this unit, including presets and sequences, with the backup." }),
    dim(PUT_IN_DOWNLOAD));
}

function requestBackup(u, opener) {
  showConfirm({ action: "backup", unitId: u.unit_id, label: title(u) },
    "BACK UP THIS UNIT?", "BACK UP", opener,
    el("div", {}, el("b", { text: title(u) }), u.firmware ? ` · ${u.firmware}` : ""),
    el("div", { text: "Saves a copy of everything on the unit. Nothing on it is changed." }),
    dim(PUT_IN_DOWNLOAD));
}

function requestDelete(u, b, opener) {
  showConfirm({ action: "delete", unitId: u.unit_id, label: title(u), backup: b.id },
    "DELETE THIS BACKUP?", "DELETE", opener,
    el("div", {}, el("b", { text: title(u) }), ` · ${b.firmware || b.id} · saved ${b.saved}`),
    el("div", { class: "warn", text: "This cannot be undone." }));
}

// DEL opens this chooser, so a delete is never one tap from the list: the choice is RESTORE
// or DELETE, and each of those then has its own confirmation. The chooser is armed like the
// confirm (ignores clicks for ARM_MS) and its actions row goes away from the opener.
function openChooser(u, b, opener) {
  state.chooser = { u, b, opener, armedAt: performance.now() };
  const { cx, cy } = openerCentre(opener);
  const plan = actionPlan(cx, cy, window.innerWidth, window.innerHeight);
  $("#chooser-box").className = plan.top ? "modal-box actions-top" : "modal-box";
  $("#chooser-actions").className = "modal-actions" + (plan.goLeft ? " go-left" : "");
  $("#chooser-title").textContent = "BACKUP: " + title(u).toUpperCase();
  $("#chooser-body").replaceChildren(
    el("div", {}, el("b", { text: b.firmware || b.id }), ` · saved ${b.saved}`),
    el("div", { text: "RESTORE writes it to the unit. DELETE removes it from the Pi." }),
  );
  $("#chooser").hidden = false;
  status("Choose RESTORE or DELETE, or CANCEL.");
}

function closeChooser() {
  $("#chooser").hidden = true;
  state.chooser = null;
}

// Each choice closes the chooser and opens its own confirmation, carrying the DEL press's
// geometry so the confirm also keeps away from the opener.
function chooserChoose(action) {
  const c = state.chooser;
  if (!c) return;
  if (performance.now() - c.armedAt < ARM_MS) return;
  closeChooser();
  if (action === "restore") requestRestore(c.u, c.b, c.opener);
  else requestDelete(c.u, c.b, c.opener);
}

async function restartUnit(u) {
  state.busy = true;
  state.phase = "Restarting…";
  render();
  let result;
  try {
    await api("/api/restart", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ unit_id: u.unit_id, confirm: u.unit_id }),
    });
    result = `${title(u)} restarted.`;
  } catch (err) {
    result = `FAILED: ${err.message}`;
  }
  state.busy = false;
  state.phase = "";
  state.notice = true;
  try { await loadFleet(); } catch { render(); }
  status(result);
}

// Only a firmware you can SEE may be flashed. Turning the page used to keep the selection,
// so FLASH stayed live for a row that was no longer on screen (2026-11-22: ChoralRoot was
// installed on unit C when the operator meant a firmware on the next page).
function changePage(delta) {
  state.page += delta;
  state.pkg = null;
  render();
}

function selectFirmware(name) {
  state.notice = false;
  state.pkg = name;
  render();          // the footer names the selection
}

// ------------------------------------------------------------------ settings

async function renderSettings(screen) {
  const gen = state.gen;
  let items = [];
  try { items = (await api("/api/flashlog")).items || []; } catch { /* history is optional */ }
  if (gen !== state.gen) return;
  const label = (id) => {
    const u = state.units.find((x) => x.unit_id === id);
    return u && u.label ? title(u) : `…${String(id || "").slice(-4)}`;
  };
  const row = (r) => {
    const what = r.event === "restart" ? "restarted"
      : r.event === "rename" ? `named ${r.message}`
      : r.event === "bind" ? `registered as ${r.message}`
      : r.event === "reconcile" ? `units matched: ${r.message || ""}`
      : r.event === "backup-deleted" ? "backup deleted"
      : r.event === "refused" ? `REFUSED: ${r.message || ""}`
      : r.event === "setup-pending" ? `${(r.package || "").replace(/\.fwsc$/, "")} — SETUP UNFINISHED`
      : r.event === "setup-replaced" ? `${(r.package || "").replace(/\.fwsc$/, "")} — replaced by another firmware`
      : r.event === "setup-finished" ? `${(r.package || "").replace(/\.fwsc$/, "")} — setup finished`
      : r.event === "failed" ? `WRITE FAILED (may be partial): ${r.message || ""}`
      : `${(r.package || "").replace(/\.fwsc$/, "")} — ${r.message || "done"}`;
    return el("div", { class: "fwrow", text: `${(r.at || "").slice(5, 16)}  ${label(r.unit_id)}  ${what}` });
  };
  screen.append(el("div", { class: "list" },
    el("div", { class: "fwrow", text: items.length ? "RECENT ACTIVITY" : "NO ACTIVITY RECORDED YET" }),
    ...items.slice(0, 2).map(row),   // the panel is 480px tall
  ));
  screen.append(el("div", { class: "actions" },
    el("button", { class: "ghost", text: "REGISTER…", disabled: writing(),
                   onclick: () => openRegister() }),
    el("button", { class: "ghost", text: "GET FIRMWARE…", disabled: writing(),
                   onclick: () => { state.gpage = 0; state.getlist = null; state.view = "get"; render(); } })));
}

// ------------------------------------------------------------------ confirm + write

function requestFlash(u, sel, opener) {
  if (!sel || !hasFlashId(u)) return;
  const p = sel.p;
  showConfirm({ action: "flash", unitId: u.unit_id, pkgName: p.name, sha: p.sha256, label: title(u) },
    "FLASH THIS UNIT?", "WRITE", opener,
    el("div", {}, el("b", { text: title(u) }), u.firmware ? ` · currently ${u.firmware}` : ""),
    el("div", {}, el("b", { text: p.name }), p.identity ? ` · ${p.identity}` : ""),
    sel.compat && sel.compat.severity === "warn" ? el("div", { class: "warn", text: sel.compat.note }) : null,
    sel.p.severity !== "ok"
      ? el("div", { class: "warn", text: "TWO STEPS. 1: this writes the firmware. 2: then finish the " +
          "maker's installer on a laptop (Chrome or Edge, USB). Press FINISH SETUP afterwards; until " +
          "then the unit shows SETUP UNFINISHED." })
      : null,
    u.setup_pending && sel.p.severity === "ok"
      ? el("div", { class: "warn", text: `${title(u)} is SETUP UNFINISHED for ` +
          `${String(u.setup_pending.package).replace(/\.fwsc$/, "")}. This write replaces it.` })
      : null,
    el("div", { style: "margin-top:10px;color:var(--dim);font-size:20px",
                text: "Settings, presets and sequences are not erased. The unit restarts when the write finishes." }),
    u.download ? null : dim(PUT_IN_DOWNLOAD));
}

// The Pi runs the maker's own installer for this unit. The server saves a backup first, and clears
// the flag only when the unit itself reports the missing part present.
function requestPiInstall(u, opener) {
  const pkg = String(u.setup_pending?.package || "").replace(/\.fwsc$/, "");
  showConfirm({ action: "setup-install", unitId: u.unit_id, label: title(u) },
    "INSTALL FROM THE PI?", "INSTALL", opener,
    el("div", {}, el("b", { text: title(u) }), pkg ? ` · ${pkg}` : ""),
    el("div", { style: "margin-top:10px;color:var(--dim);font-size:20px",
                text: "Saves a backup, then runs the maker's installer on this unit. It takes a few " +
                      "minutes. Do not unplug anything. Every unit must be on." }));
}

// The operator says the maker's installer has finished. Clears the flag only; nothing is written.
function requestFinish(u, opener) {
  const pkg = String(u.setup_pending?.package || "").replace(/\.fwsc$/, "");
  showConfirm({ action: "finish-setup", unitId: u.unit_id, label: title(u) },
    "FINISH SETUP?", "CONFIRM", opener,
    el("div", {}, el("b", { text: title(u) }), pkg ? ` · ${pkg}` : ""),
    el("div", { style: "margin-top:10px;color:var(--dim);font-size:20px",
                text: "Confirm only after the maker's installer has finished on this unit. " +
                      "Nothing is written to the unit by this step." }));
}

function closeConfirm() {
  $("#confirm").hidden = true;
  state.pending = null;
  state.pressOnGo = false;
}

// The stored LAN token, if the operator saved one (see secureToken below). The panel on
// the Pi's own loopback never needs a token; this only matters when the server is
// deliberately bound beyond loopback (audit SEC-01/02), and then every mutation must
// carry X-FM1-Token.
function savedToken() {
  try { return localStorage.getItem("fm1-token") || ""; } catch { return ""; }
}

const postJson = async (path, body, retried = false) => {
  const token = savedToken();
  try {
    return await api(path, {
      method: "POST",
      headers: Object.assign({ "Content-Type": "application/json" },
                             token ? { "X-FM1-Token": token } : {}),
      body: JSON.stringify(body),
    });
  } catch (err) {
    // Headless use from a phone or tablet: the Pi wants its write token once. Ask, keep it
    // on this device, and retry the same request a single time. The panel on the Pi's own
    // loopback never gets a 401, so it never sees this.
    if (!retried && /X-FM1-Token/i.test(err.message) && askToken()) {
      return postJson(path, body, true);
    }
    throw err;
  }
};

function askToken() {
  let value = null;
  try {
    value = window.prompt("WRITE TOKEN (on the Pi: ~/.config/fm1/token, or printed by fm1-web)");
  } catch { return false; }
  value = (value || "").trim();
  if (!value) return false;
  try { localStorage.setItem("fm1-token", value); } catch { /* private mode: asks again next time */ }
  return true;
}

// Run one confirmed act and say, in a line, what happened. Throws on a refusal.
async function perform(pend) {
  if (pend.action.startsWith("router-")) return performRouter(pend);
  if (pend.action.startsWith("set-")) return performSets(pend);
  const confirm = pend.unitId;
  if (pend.action === "bind") {
    // The confirm IS the flash id (the server refuses anything else), and the server
    // re-reads the unit in download mode during the bind: a stale scan cannot bind.
    // The letter in the answer is what the server actually assigned, not what we showed.
    const res = await postJson("/api/register/bind", { unit_id: pend.unitId, confirm });
    return `Unit …${pend.unitId.slice(-4)} is now sticker ${res.label !== undefined ? res.label : pend.letter}. Tap it to name it or flash it.`;
  }
  if (pend.action === "clock-set") {
    const res = await postJson("/api/clock/set", { confirm: "clock", epoch: Math.floor(Date.now() / 1000) });
    await loadFleet();
    return (res.message || "done").toUpperCase();
  }
  if (pend.action === "reconcile") {
    const res = await postJson("/api/reconcile", { confirm: "reconcile", all: true });
    return (res.message || "done").toUpperCase();
  }
  if (pend.action === "setup-install") {
    const res = await postJson("/api/setup/install", { unit_id: pend.unitId, confirm: pend.unitId });
    return `${pend.label}: ${res.message || "setup finished"}.`;
  }
  if (pend.action === "finish-setup") {
    await postJson("/api/setup/finish", { unit_id: pend.unitId, confirm: pend.unitId });
    return `${pend.label}: setup marked finished.`;
  }
  if (pend.action === "restore") {
    await postJson("/api/restore", { unit_id: pend.unitId, backup: pend.backup, confirm });
    return `${pend.label}: restored ${pend.pkgName}.`;
  }
  if (pend.action === "backup") {
    await postJson("/api/backup", { unit_id: pend.unitId, confirm });
    return `${pend.label}: backup saved.`;
  }
  if (pend.action === "delete") {
    await postJson("/api/backup/delete", { unit_id: pend.unitId, backup: pend.backup, confirm });
    return "Backup deleted.";
  }
  if (pend.action === "multi") return performMulti(pend);
  await postJson("/api/flash", { unit_id: pend.unitId, package: pend.pkgName, sha256: pend.sha, confirm });
  return `${pend.label}: ${pend.pkgName} installed.`;
}

// Several units, strictly one after another. Each is a full id-matched write of its own (the
// server checks every one), and the first failure stops the run so a problem is never
// compounded across the fleet.
async function performMulti(pend) {
  const done = [];
  try {
    for (const [i, t] of pend.units.entries()) {
      state.multi = { i: i + 1, n: pend.units.length, label: t.letter };
      state.phase = "Starting…";
      status(progressText());
      try {
        await postJson("/api/flash", { unit_id: t.unitId, package: pend.pkgName, sha256: pend.sha,
                                       confirm: t.unitId });
      } catch (err) {
        const left = pend.units.slice(i + 1).map((x) => x.letter).join(" ");
        // A result list, one entry per unit: A ✓ B ✗ C – D –
        const rest = pend.units.slice(i + 1).map((x) => `${x.letter} –`);
        return [...done.map((l) => `${l} ✓`), `${t.letter} ✗`, ...rest].join("  ")
             + `. ${t.letter} FAILED: ${err.message}`;
      }
      done.push(t.letter);
    }
  } finally {
    state.multi = null;
  }
  return `${pend.pkgName}: ${done.map((l) => `${l} ✓`).join("  ")}`;
}

// How long a freshly opened dialog ignores clicks. Long enough to swallow the repeats of
// the press that opened it (a stationary finger on a resistive panel keeps issuing them);
// far below a human tap-to-tap gap, so it never gets in the operator's way.
const ARM_MS = 600;

async function doWrite() {
  const pend = state.pending;
  if (!pend) return;
  // Guard 1 -- arm: the repeats of the press that opened this dialog land inside the
  // first ARM_MS. Guard 2 -- press origin: a click is honoured only when its own press
  // started on the confirm button. Together with opener-aware geometry they make it so a
  // held press on an opener can never arm AND confirm a write (audit UX-01).
  if (performance.now() - state.armedAt < ARM_MS) return;
  if (!state.pressOnGo) return;
  closeConfirm();
  // Lock the screen BEFORE the request: the write takes a minute or more, and until now the
  // FLASH button stayed live-looking for all of it.
  state.busy = true;
  render();
  state.phase = "Starting…";
  status(progressText());
  let result;
  try {
    result = await perform(pend);
  } catch (err) {
    result = `FAILED: ${err.message}`;
  }
  // loadFleet() re-renders, and render() would replace the result with the idle hint.
  //
  // ORDER MATTERS: `busy` is cleared BEFORE the refresh so the screen is already unlocked
  // whatever the refresh returns.
  state.busy = false;
  state.serverBusy = false;
  state.phase = "";
  state.notice = true;
  if (pend.action === "restore" || pend.action === "multi") state.view = "unit";
  if (pend.action === "bind") { state.view = "fleet"; state.regscan = null; }
  if (pend.action.startsWith("router-")) {
    if (pend.after) state.view = pend.after;
    try { await loadRouter(); } catch { /* the next poll shows what the router says */ }
  }
  if (pend.action.startsWith("set-")) {
    if (pend.after) state.view = pend.after;
    try { await loadSets(); } catch { /* the next poll shows what the server says */ }
  }
  try {
    await loadFleet();
  } catch {
    // The refresh failed, so the screen is still drawn for the old state. Redraw it now so
    // the write result is not shown behind locked controls.
    render();
  }
  // The refresh awaits loadLibrary(); a render after it could otherwise replace this line.
  status(result);
}

// ------------------------------------------------------------------ several units

// Units a write could be addressed to right now: a readable flash id, and present.
function flashable() { return state.units.filter((x) => hasFlashId(x) && (x.running || x.download)); }

function openMulti(u) {
  state.picked = new Set([u.unit_id]);
  state.view = "multi";
  render();
}

async function renderMulti(screen) {
  const name = state.pkg;
  if (!name) { state.view = "unit"; render(); return; }
  const pick = el("div", { class: "pick" });
  for (const x of flashable()) {
    const on = state.picked.has(x.unit_id);
    pick.append(el("button", {
      class: "fwrow" + (on ? " selected" : ""),
      text: `${on ? "✓" : "·"} ${x.label}${x.name ? " " + x.name : ""}`,
      onclick: () => { on ? state.picked.delete(x.unit_id) : state.picked.add(x.unit_id); render(); },
    }));
  }
  screen.append(el("div", { class: "fwrow", text: `FLASH ${name.replace(/\.fwsc$/, "")} TO:` }), pick);
  const n = state.picked.size;
  screen.append(el("div", { class: "actions" },
    el("button", { class: "ghost", text: "BACK", onclick: () => { state.view = "unit"; render(); } }),
    el("button", { class: "primary", text: n ? `FLASH ${n} UNIT${n === 1 ? "" : "S"}` : "PICK UNITS",
                   disabled: !n || writing(), onclick: (ev) => requestMulti(ev) })));
}

async function requestMulti(opener) {
  const p = state.packages.find((x) => x.name === state.pkg);
  const targets = flashable().filter((x) => state.picked.has(x.unit_id));
  if (!p || !targets.length) return;
  const warns = [];
  for (const x of targets) {
    const c = await compatFor(x, p.name);
    if (c && c.severity === "warn") warns.push(el("div", { class: "warn", text: `${x.label}: ${c.note}` }));
  }
  showConfirm({ action: "multi", pkgName: p.name, sha: p.sha256, label: "several",
                units: targets.map((x) => ({ unitId: x.unit_id, letter: x.label })) },
    `FLASH ${targets.length} UNITS?`, "WRITE", opener,
    el("div", {}, el("b", { text: p.name }), p.identity ? ` · ${p.identity}` : ""),
    el("div", {}, "To: ", el("b", { text: targets.map((x) => title(x)).join(", ") })),
    ...warns,
    dim("One after another, about 35 seconds each. It stops at the first problem. Settings and presets are not erased."));
}

// ------------------------------------------------------------------ names

// A touch panel has no keyboard, so names are picked, not typed. (Anything the server accepts
// can still be set over the web admin or the API.)
const NAMES = ["BASS", "LEAD", "PADS", "KEYS", "DRUMS", "PERC", "ARP", "FX", "SEQ", "VOX"];
const NAME_MAX = 12;   // the server's limit (registry.NAME_MAX); it checks again

async function renderName(screen) {
  const u = unitById(state.selected);
  if (!u) { state.view = "fleet"; render(); return; }
  const pick = el("div", { class: "pick4" });
  for (const n of NAMES) {
    pick.append(el("button", { class: "fwrow" + (u.name === n ? " selected" : ""), text: n,
                               onclick: () => setName(u, n) }));
  }
  pick.append(el("button", { class: "fwrow", text: "TYPE…",
                             onclick: () => { state.typed = u.name || ""; state.view = "type"; render(); } }));
  pick.append(el("button", { class: "fwrow", text: "CLEAR", onclick: () => setName(u, "") }));
  screen.append(pick, el("div", { class: "actions" },
    el("button", { class: "ghost", text: "BACK", onclick: () => { state.view = "unit"; render(); } })));
  if (!state.notice) status(`NAME UNIT ${u.label}${u.name ? " (NOW " + u.name + ")" : ""}`);
}

// On-screen keyboard: for a name that is not in the list. Letters, digits, space and - . _ +.
const KEYROWS = ["1234567890", "QWERTYUIOP", "ASDFGHJKL-", "ZXCVBNM._+"];

function buildKeyboard(press) {
  const kb = el("div", { class: "kb" });
  for (const row of KEYROWS) {
    const r = el("div", { class: "kbrow" });
    for (const ch of row) r.append(el("button", { class: "key", text: ch, onclick: () => press(ch) }));
    kb.append(r);
  }
  return kb;
}

function renderType(screen) {
  const u = unitById(state.selected);
  if (!u) { state.view = "fleet"; render(); return; }
  state.typed = state.typed || "";
  const shown = el("div", { class: "typed", text: state.typed + "▏" });
  const press = (ch) => {
    if (state.typed.length < NAME_MAX) state.typed += ch;
    shown.textContent = state.typed + "▏";
  };
  const kb = buildKeyboard(press);
  screen.append(shown, kb, el("div", { class: "actions" },
    el("button", { class: "ghost", text: "CANCEL", onclick: () => { state.view = "name"; render(); } }),
    el("button", { class: "ghost", text: "SPACE", onclick: () => press(" ") }),
    el("button", { class: "ghost", text: "⌫", onclick: () => {
      state.typed = state.typed.slice(0, -1); shown.textContent = state.typed + "▏"; } }),
    el("button", { class: "primary", text: "SAVE", onclick: () => setName(u, state.typed) }),
  ));
  if (!state.notice) status(`TYPE A NAME FOR UNIT ${u.label} (UP TO ${NAME_MAX})`);
}

async function setName(u, name) {
  try {
    await postJson("/api/rename", { unit_id: u.unit_id, name, confirm: u.unit_id });
  } catch (err) {
    state.notice = true;
    status(`FAILED: ${err.message}`);
    return;
  }
  state.view = "unit";
  state.notice = true;
  try { await loadFleet(); } catch { render(); }
  status(name ? `${u.label} IS NOW ${name}.` : `NAME CLEARED FROM ${u.label}.`);
}


// ------------------------------------------------------------------ routes (dawless hub)
//
// Patches of MIDI routes between the units, run by the router on the Pi. This page edits
// two files the router follows (the saved patches, and which patch is chosen to run) and can
// send "all sound off". It never writes to a unit's flash, so edits need no write dialog;
// the acts that change what is playing or lose work (RUN, STOP, DELETE, PANIC) still confirm.

async function loadRouter() {
  state.router = await api("/api/router");
  state.rsig = JSON.stringify(state.router);
}

// Quiet poll while a routes screen is open: redraw only when the router's answer changed, so a
// half-made choice is not rebuilt under a finger.
async function refreshRouter() {
  if (state.pending || state.busy) return;
  const before = state.rsig;
  await loadRouter();
  if (state.rsig !== before && ["routes", "patch", "clock"].includes(state.view)) render();
}

async function openRoutes() {
  state.notice = false;
  state.ppage = 0;
  try { await loadRouter(); } catch (err) { state.notice = true; status(`ROUTES UNAVAILABLE: ${err.message}`); return; }
  state.view = "routes";
  render();
}

const routerPatch = (name) => ((state.router && state.router.patches) || []).find((p) => p.name === name) || null;
const slotLabel = (slot) => {
  const s = ((state.router && state.router.slots) || []).find((x) => x.slot === slot);
  return s ? s.label : slot;
};
// The patch the router is (or will be) running: the panel's choice, else the configured default.
function effectivePatch() {
  const d = state.router;
  return d.active === null || d.active === undefined ? d.default : d.active;
}

function routerLine() {
  const d = state.router;
  if (d.error) return `PATCH FILE PROBLEM: ${d.error}`.toUpperCase();
  const rt = d.router;
  if (!rt.running) return "ROUTER NOT RUNNING: PATCHES ARE SAVED, NOTHING IS ROUTED";
  const bits = [`ROUTER RUNNING: ${rt.patch === "idle" ? "STOPPED" : rt.patch.toUpperCase()}`];
  if (rt.attached.length) bits.push("LIVE " + rt.attached.map(slotLabel).join(" "));
  if (rt.missing.length) bits.push("NOT FOUND " + rt.missing.map(slotLabel).join(" "));
  if (rt.held.length) bits.push("FREED FOR A FLASH " + rt.held.map(slotLabel).join(" "));
  return bits.join(" · ");
}

const PATCH_ROWS = 2;

function renderRoutes(screen) {
  const d = state.router;
  if (!d) { state.view = "fleet"; render(); return; }
  const eff = effectivePatch();
  const rows = d.patches;
  const per = rowsPer(PATCH_ROWS);
  const pages = Math.max(1, Math.ceil(rows.length / per));
  state.ppage = Math.max(0, Math.min(state.ppage, pages - 1));
  const list = el("div", { class: "list" },
    el("div", { class: "fwrow", text: routerLine() }));
  if (!rows.length) list.append(el("div", { class: "fwrow", text: "NO PATCHES YET: TAP NEW PATCH" }));
  for (const p of rows.slice(state.ppage * per, (state.ppage + 1) * per)) {
    list.append(el("button", { class: "fwrow" + (p.name === eff ? " selected" : ""),
                               onclick: () => openPatch(p.name) },
      el("span", { text: `${p.name === eff ? "● " : ""}${p.name}` }),
      el("span", { class: "mark unknown", text: `${p.routes.length} ROUTE${p.routes.length === 1 ? "" : "S"}` })));
  }
  screen.append(list, el("div", { class: "actions" },
    el("button", { class: "primary", text: "NEW PATCH", disabled: writing(),
                   onclick: () => { state.view = "pname"; render(); } }),
    el("button", { class: "ghost", text: clockButtonText(), disabled: writing(),
                   onclick: () => { state.notice = false; state.view = "clock"; render(); } }),
    el("button", { class: "danger", text: "PANIC", disabled: writing(), onclick: (ev) => requestPanic(ev) }),
    pages > 1 ? el("button", { class: "ghost", text: `‹ ${state.ppage + 1}/${pages}`, disabled: state.ppage === 0,
                               onclick: () => { state.ppage--; render(); } }) : null,
    pages > 1 ? el("button", { class: "ghost", text: "›", disabled: state.ppage >= pages - 1,
                               onclick: () => { state.ppage++; render(); } }) : null));
  if (!state.notice && !writing()) status("● = RUNS ON THE ROUTER");
}

// --- the master clock: tempo and start/stop, sent to the units by the router

const clockOf = () => (state.router && state.router.clock) || null;
const clockButtonText = () => {
  const c = clockOf();
  return c && c.running ? `CLOCK ${c.bpm} ▶` : "CLOCK";
};

// Tempo and targets are only a file edit, so they apply at once and need no dialog. START makes
// every target unit's sequencer run, so it confirms; STOP does not.
async function setClock(change, okText) {
  try {
    const res = await postJson("/api/router/clock", change);
    state.router.clock = Object.assign({}, state.router.clock, res.clock);
    state.rsig = JSON.stringify(state.router);
    if (okText) status(okText);
  } catch (err) {
    state.notice = true;
    status(`CLOCK NOT CHANGED: ${err.message}`);
  }
  render();
}

function clockLine(c) {
  if (c.error) return `CLOCK FILE PROBLEM: ${c.error}`.toUpperCase();
  const who = c.targets.length ? c.targets.map(slotLabel).join(" ") : "EVERY UNIT";
  const bits = [`${c.bpm} BPM`, c.running ? "RUNNING" : "STOPPED", `TO ${who}`];
  if (!state.router.router.running) bits.push("ROUTER NOT RUNNING: NOTHING IS SENT");
  return bits.join(" · ");
}

function renderClock(screen) {
  const c = clockOf();
  if (!c) { state.view = "routes"; render(); return; }
  const nudge = (d) => () => setClock({ bpm: Math.max(c.min, Math.min(c.max, Math.round((c.bpm + d) * 10) / 10)) });
  const row = el("div", { class: "actions" });
  for (const d of [-10, -1, 1, 10]) {
    row.append(el("button", { class: "ghost", text: d > 0 ? `+${d}` : `−${-d}`, disabled: writing(), onclick: nudge(d) }));
  }
  const pick = el("div", { class: "pick4" });
  for (const s of state.router.slots) {
    const on = c.targets.length === 0 || c.targets.includes(s.slot);
    pick.append(el("button", { class: "fwrow" + (on ? " selected" : ""), text: `${on ? "✓ " : ""}${s.label}`, disabled: writing(),
      onclick: () => {
        // Nothing ticked means every unit; ticking all of them is the same, so it is stored empty.
        const all = state.router.slots.map((x) => x.slot);
        const now = c.targets.length ? c.targets : all;
        let next = now.includes(s.slot) ? now.filter((x) => x !== s.slot) : now.concat(s.slot);
        if (!next.length) return; // at least one unit must stay ticked
        if (next.length === all.length) next = [];
        setClock({ targets: next });
      } }));
  }
  screen.append(
    el("div", { class: "fwrow", style: "justify-content:center", text: `TEMPO ${c.bpm} BPM` }), row,
    el("div", { class: "fwrow", text: "SEND CLOCK TO" }), pick,
    el("div", { class: "actions" },
      c.running
        ? el("button", { class: "danger", text: "STOP", disabled: writing(), onclick: () => setClock({ running: false }, "CLOCK STOPPED.") })
        : el("button", { class: "primary", text: "START", disabled: writing(), onclick: (ev) => requestClockStart(ev) })));
  if (!state.notice && !writing()) status(clockLine(c));
}

function requestClockStart(opener) {
  const c = clockOf();
  const who = c.targets.length ? c.targets.map(slotLabel).join(" ") : "every running unit";
  showConfirm({ action: "router-clock-start", after: "clock" },
    "START THE CLOCK?", "START", opener,
    el("div", {}, el("b", { text: `${c.bpm} BPM` }), ` · ${who}`),
    el("div", { text: "Sends MIDI start, then clock, to those units. Anything on them that follows MIDI clock runs." }),
    dim("The router must be running. STOP sends MIDI stop."));
}

function openPatch(name) {
  state.rpatch = name;
  state.rtpage = 0;
  state.notice = false;
  state.view = "patch";
  render();
}

function renderPatch(screen) {
  const p = routerPatch(state.rpatch);
  if (!p) { state.view = "routes"; render(); return; }
  const running = effectivePatch() === p.name;
  const per = rowsPer(PATCH_ROWS);
  const pages = Math.max(1, Math.ceil(p.routes.length / per));
  state.rtpage = Math.max(0, Math.min(state.rtpage, pages - 1));
  const list = el("div", { class: "list" },
    el("div", { class: "fwrow", text: `${p.name}${running ? " · RUNS ON THE ROUTER" : " · NOT CHOSEN"}` }));
  if (!p.routes.length) list.append(el("div", { class: "fwrow", text: "NO ROUTES: TAP ADD ROUTE" }));
  for (const r of p.routes.slice(state.rtpage * per, (state.rtpage + 1) * per)) {
    list.append(el("div", { class: "pair" },
      el("div", { class: "fwrow", text: r.text }),
      el("button", { class: "fwrow del", text: "DEL", disabled: writing(),
                     onclick: (ev) => requestRouteDelete(p, r, ev) })));
  }
  screen.append(list, el("div", { class: "actions" },
    el("button", { class: "primary", text: "ADD ROUTE", disabled: writing(), onclick: () => startRoute(p.name) }),
    running
      ? el("button", { class: "ghost", text: "STOP ROUTING", disabled: writing(), onclick: (ev) => requestActivate(p.name, true, ev) })
      : el("button", { class: "ghost", text: "RUN THIS", disabled: writing() || !p.routes.length,
                       onclick: (ev) => requestActivate(p.name, false, ev) }),
    el("button", { class: "ghost", text: "DELETE…", disabled: writing(), onclick: (ev) => requestPatchDelete(p, ev) }),
    pages > 1 ? el("button", { class: "ghost", text: `‹ ${state.rtpage + 1}/${pages}`, disabled: state.rtpage === 0,
                               onclick: () => { state.rtpage--; render(); } }) : null,
    pages > 1 ? el("button", { class: "ghost", text: "›", disabled: state.rtpage >= pages - 1,
                               onclick: () => { state.rtpage++; render(); } }) : null));
  if (!state.notice && !writing()) status(routerLine());
}

// --- making a route

function startRoute(patchName) {
  state.rpatch = patchName;
  state.draft = { source: null, dest: null, remap: 0, transpose: 0, clock: false, cc: false };
  state.notice = false;
  state.view = "routeadd";
  render();
}

function draftRoute(dr) {
  const route = { source: dr.source, dest: dr.dest };
  if (dr.remap) route.remap_to = dr.remap;
  if (dr.transpose) route.transpose = dr.transpose;
  const block = [dr.clock ? "clock" : null, dr.cc ? "cc" : null].filter(Boolean);
  if (block.length) route.block = block;
  return route;
}

const draftOk = (dr) => !!(dr.source && dr.dest && dr.source !== dr.dest);

function slotPicker(label, field, other) {
  const dr = state.draft;
  const row = el("div", { class: "pick4" });
  for (const s of state.router.slots) {
    row.append(el("button", {
      class: "fwrow" + (dr[field] === s.slot ? " selected" : ""), text: s.label,
      disabled: s.slot === dr[other],
      onclick: () => { dr[field] = s.slot; render(); } }));
  }
  return [el("div", { class: "fwrow", text: label }), row];
}

function routeSaveButton() {
  return el("button", { class: "primary", text: "ADD ROUTE", disabled: !draftOk(state.draft) || writing(),
                        onclick: () => saveRoute() });
}

function renderRouteAdd(screen) {
  if (!state.draft || !state.router) { state.view = "routes"; render(); return; }
  const dr = state.draft;
  screen.append(...slotPicker("FROM UNIT", "source", "dest"), ...slotPicker("TO UNIT", "dest", "source"),
    el("div", { class: "actions" },
      el("button", { class: "ghost", text: "OPTIONS…", onclick: () => { state.view = "routeopt"; render(); } }),
      routeSaveButton()));
  if (!state.notice && !writing()) {
    status(draftOk(dr) ? `${slotLabel(dr.source)} → ${slotLabel(dr.dest)}: ADD IT, OR SET OPTIONS FIRST`
                       : "PICK THE UNIT THAT SENDS, AND THE UNIT THAT RECEIVES");
  }
}

function stepper(label, value, shown, down, up) {
  return el("div", { class: "actions" },
    el("button", { class: "ghost", style: "flex:0 0 calc(22 * var(--u))", text: "−", onclick: down }),
    el("div", { class: "fwrow", style: "flex:1 1 auto;justify-content:center", text: `${label}: ${shown(value)}` }),
    el("button", { class: "ghost", style: "flex:0 0 calc(22 * var(--u))", text: "+", onclick: up }));
}

function renderRouteOpt(screen) {
  if (!state.draft) { state.view = "routes"; render(); return; }
  const dr = state.draft;
  const step = (field, lo, hi, d) => () => { dr[field] = Math.max(lo, Math.min(hi, dr[field] + d)); render(); };
  screen.append(
    stepper("SEND ON CHANNEL", dr.remap, (v) => (v ? String(v) : "AS PLAYED"), step("remap", 0, 16, -1), step("remap", 0, 16, 1)),
    stepper("TRANSPOSE", dr.transpose, (v) => (v > 0 ? `+${v}` : String(v)), step("transpose", -24, 24, -12), step("transpose", -24, 24, 12)),
    el("div", { class: "pick" },
      el("button", { class: "fwrow" + (dr.clock ? " selected" : ""), text: `${dr.clock ? "✓" : "·"} BLOCK CLOCK`,
                     onclick: () => { dr.clock = !dr.clock; render(); } }),
      el("button", { class: "fwrow" + (dr.cc ? " selected" : ""), text: `${dr.cc ? "✓" : "·"} BLOCK CC`,
                     onclick: () => { dr.cc = !dr.cc; render(); } })),
    el("div", { class: "actions" },
      el("button", { class: "ghost", text: "DONE", onclick: () => { state.view = "routeadd"; render(); } }),
      routeSaveButton()));
  if (!state.notice && !writing()) status("TRANSPOSE MOVES BY AN OCTAVE PER TAP. MORE (RANGE, VELOCITY) IS IN THE ROUTE CLI");
}

async function saveRoute() {
  const dr = state.draft;
  if (!dr || !draftOk(dr)) return;
  const name = state.rpatch;
  let result;
  state.busy = true;
  render();
  try {
    const res = await postJson("/api/router/route", { patch: name, route: draftRoute(dr) });
    result = `${name}: ${res.route} ADDED.`;
    state.view = "patch";
    state.draft = null;
  } catch (err) {
    result = `NOT ADDED: ${err.message}`;
  }
  state.busy = false;
  state.notice = true;
  try { await loadRouter(); } catch { /* the next poll shows it */ }
  render();
  status(result);
}

// --- naming a patch (picked, or typed on the on-screen keyboard)

const PATCH_NAMES = ["JAM", "DRONE", "LIVE", "SEQ", "BASS", "LEAD", "PADS", "TEST"];
const PATCH_NAME_MAX = 16;

function renderPatchName(screen) {
  const pick = el("div", { class: "pick4" });
  for (const n of PATCH_NAMES) {
    pick.append(el("button", { class: "fwrow", text: n, onclick: () => startRoute(n) }));
  }
  pick.append(el("button", { class: "fwrow", text: "TYPE…",
                             onclick: () => { state.typed = ""; state.view = "ptype"; render(); } }));
  screen.append(pick);
  if (!state.notice) status("NAME THE PATCH. A NAME ALREADY IN USE ADDS A ROUTE TO THAT PATCH");
}

function renderPatchType(screen) {
  state.typed = state.typed || "";
  const shown = el("div", { class: "typed", text: state.typed + "▏" });
  const press = (ch) => {
    if (state.typed.length < PATCH_NAME_MAX) state.typed += ch;
    shown.textContent = state.typed + "▏";
  };
  screen.append(shown, buildKeyboard(press), el("div", { class: "actions" },
    el("button", { class: "ghost", text: "CANCEL", onclick: () => { state.view = "pname"; render(); } }),
    el("button", { class: "ghost", text: "SPACE", onclick: () => press(" ") }),
    el("button", { class: "ghost", text: "⌫", onclick: () => {
      state.typed = state.typed.slice(0, -1); shown.textContent = state.typed + "▏"; } }),
    el("button", { class: "primary", text: "NEXT", onclick: () => {
      const n = state.typed.trim();
      if (n) startRoute(n); } }),
  ));
  if (!state.notice) status(`TYPE A PATCH NAME (UP TO ${PATCH_NAME_MAX})`);
}

// --- confirmed acts

function requestRouteDelete(p, r, opener) {
  showConfirm({ action: "router-route-delete", patch: p.name, index: r.index, after: "patch" },
    "REMOVE THIS ROUTE?", "REMOVE", opener,
    el("div", {}, el("b", { text: p.name }), ` · ${r.text}`),
    dim("Only the saved patch changes. No unit is touched."));
}

function requestPatchDelete(p, opener) {
  showConfirm({ action: "router-patch-delete", patch: p.name, after: "routes" },
    "DELETE THIS PATCH?", "DELETE", opener,
    el("div", {}, el("b", { text: p.name }), ` · ${p.routes.length} route${p.routes.length === 1 ? "" : "s"}`),
    el("div", { class: "warn", text: "This cannot be undone." }));
}

function requestActivate(name, stop, opener) {
  showConfirm({ action: "router-activate", patch: stop ? "" : name, after: "patch" },
    stop ? "STOP ROUTING?" : "RUN THIS PATCH?", stop ? "STOP" : "RUN", opener,
    el("div", {}, el("b", { text: name })),
    el("div", { text: stop ? "The router lets go of every unit. Nothing is passed between them."
                           : "The router connects the units named in it and passes MIDI between them." }),
    dim("Flashing or restarting a unit frees it automatically first."));
}

function requestPanic(opener) {
  showConfirm({ action: "router-panic", after: "routes" },
    "ALL SOUND OFF?", "SEND", opener,
    el("div", { text: "Sends sound off, reset and notes off on all 16 channels to every running unit." }),
    dim("Silences stuck notes. Nothing on any unit is changed."));
}

async function performRouter(pend) {
  if (pend.action === "router-route-delete") {
    const r = await postJson("/api/router/route/delete", { patch: pend.patch, index: pend.index, confirm: pend.patch });
    return `${pend.patch}: ${r.route} REMOVED.`;
  }
  if (pend.action === "router-patch-delete") {
    await postJson("/api/router/patch/delete", { name: pend.patch, confirm: pend.patch });
    return `PATCH ${pend.patch} DELETED.`;
  }
  if (pend.action === "router-activate") {
    await postJson("/api/router/activate", { name: pend.patch, confirm: pend.patch || "stop" });
    return pend.patch ? `${pend.patch} WILL RUN WITHIN A FEW SECONDS.` : "ROUTING STOPPED. THE UNITS ARE FREE.";
  }
  if (pend.action === "router-clock-start") {
    await postJson("/api/router/clock", { running: true, confirm: "clock" });
    return "CLOCK STARTED.";
  }
  const r = await postJson("/api/router/panic", { confirm: "panic" });
  return `SOUND OFF SENT (${r.sent} MESSAGES).`;
}

// ------------------------------------------------------------------ rig sets

// A set is a named group of firmwares (or saved images), one per unit, pushed together. It is an
// option: a set may cover one to four units, any subset can be ticked for a push, and writing a
// single unit from its own screen works exactly as before. Saving and deleting a set edit one
// file; a PUSH writes units, one at a time, through the same id-matched, backup-first path as
// every other write, and stops at the first failure.

async function loadSets() {
  state.sets = await api("/api/sets");
  state.ssig = JSON.stringify(state.sets.push) + state.sets.sets.map((x) => x.name).join();
}

// Quiet poll while a set screen is open: redraw only when the push status or the list changed.
async function refreshSets() {
  if (state.pending || state.busy) return;
  const before = state.ssig;
  await loadSets();
  if (state.ssig !== before) { if (state.view === "set") await loadPreflight(); render(); }
}

async function openSets() {
  state.notice = false;
  state.spage = 0;
  try { await loadSets(); } catch (err) { state.notice = true; status(`SETS UNAVAILABLE: ${err.message}`); return; }
  state.view = "sets";
  render();
}

const setByName = (name) => ((state.sets && state.sets.sets) || []).find((x) => x.name === name) || null;
const setSlotLabel = (slot) => {
  const s = ((state.sets && state.sets.slots) || []).find((x) => x.slot === slot);
  return s ? s.label : slot;
};
const pushing = () => !!(state.sets && state.sets.push && state.sets.push.running);

async function loadPreflight() {
  state.spre = {};
  if (!state.sname) return;
  try {
    const r = await api(`/api/sets/preflight?name=${encodeURIComponent(state.sname)}`);
    for (const st of r.steps) state.spre[st.slot] = st;
  } catch (err) { state.spre = {}; }
}

const SET_ROWS = 2;

function renderSets(screen) {
  const d = state.sets;
  if (!d) { state.view = "fleet"; render(); return; }
  const per = rowsPer(SET_ROWS);
  const pages = Math.max(1, Math.ceil(d.sets.length / per));
  state.spage = Math.max(0, Math.min(state.spage, pages - 1));
  const list = el("div", { class: "list" });
  if (d.error) list.append(el("div", { class: "fwrow", text: `SETS FILE PROBLEM: ${d.error}`.toUpperCase() }));
  if (!d.sets.length) list.append(el("div", { class: "fwrow", text: "NO SETS YET. SNAPSHOT SAVES THE RIG AS IT IS" }));
  for (const x of d.sets.slice(state.spage * per, (state.spage + 1) * per)) {
    list.append(el("button", { class: "fwrow", onclick: () => openSet(x.name) },
      el("span", { text: x.name }),
      el("span", { class: "mark unknown", text: x.members.map((m) => setSlotLabel(m.slot)).join(" ") })));
  }
  screen.append(list, el("div", { class: "actions" },
    el("button", { class: "primary", text: "SNAPSHOT", disabled: writing() || pushing(),
                   onclick: (ev) => requestSnapshot(ev) }),
    pages > 1 ? el("button", { class: "ghost", text: `‹ ${state.spage + 1}/${pages}`, disabled: state.spage === 0,
                               onclick: () => { state.spage--; render(); } }) : null,
    pages > 1 ? el("button", { class: "ghost", text: "›", disabled: state.spage >= pages - 1,
                               onclick: () => { state.spage++; render(); } }) : null));
  if (!state.notice && !writing()) status("TAP A SET TO PUSH IT, OR ANY PART OF IT");
}

async function openSet(name) {
  state.sname = name;
  state.notice = false;
  const x = setByName(name);
  state.sticked = new Set(x ? x.members.map((m) => m.slot) : []);
  await loadPreflight();
  state.view = "set";
  render();
}

// A row carries only a short tag, so four units fit above the buttons on a panel that cannot
// scroll. The full reason, and the push progress, go in the footer line (setLine).
function pushRowTag(slot) {
  const p = state.sets.push;
  if (p.set !== state.sname) return "";
  const row = p.steps.find((r) => r.slot === slot);
  return row && row.status !== "pending" ? row.status.toUpperCase() : "";
}

function setLine(x, ticked) {
  const p = state.sets.push;
  if (p.set === x.name && p.running) {
    const w = p.steps.find((r) => r.status === "writing");
    return w ? `PUSHING: WRITING ${setSlotLabel(w.slot)}…` : "PUSHING…";
  }
  if (p.set === x.name && p.finished) {
    if (p.ok) return "PUSH FINISHED: ALL UNITS WRITTEN";
    const f = p.steps.find((r) => r.status === "failed");
    return `PUSH STOPPED${f ? ` AT ${setSlotLabel(f.slot)}` : ""}: ${p.message}`.toUpperCase();
  }
  const bad = ticked.find((m) => (state.spre[m.slot] || {}).blocking);
  if (bad) return `${setSlotLabel(bad.slot)} BLOCKED: ${state.spre[bad.slot].blocking}. UNTICK IT TO PUSH THE REST`.toUpperCase();
  const warn = ticked.find((m) => (state.spre[m.slot] || {}).warning);
  if (warn) return `${setSlotLabel(warn.slot)}: ${state.spre[warn.slot].warning}`.toUpperCase();
  return "TICK THE UNITS TO WRITE; THE REST ARE LEFT ALONE";
}

function renderSet(screen) {
  const x = setByName(state.sname);
  if (!x) { state.view = "sets"; render(); return; }
  const busy = writing() || pushing();
  const list = el("div", { class: "list" });
  for (const m of x.members) {
    const pre = state.spre[m.slot] || {};
    const tag = pushRowTag(m.slot) || (pre.blocking ? "BLOCKED" : pre.warning ? "CHECK" : "");
    const on = state.sticked.has(m.slot);
    list.append(el("button", {
      class: "fwrow" + (on ? " selected" : ""), disabled: busy,
      onclick: () => { if (on) state.sticked.delete(m.slot); else state.sticked.add(m.slot); render(); } },
      el("span", { text: `${on ? "● " : "○ "}${setSlotLabel(m.slot)}  ${x.labels[m.slot]}` }),
      tag ? el("span", { class: "mark unknown", text: tag }) : null));
  }
  const ticked = x.members.filter((m) => state.sticked.has(m.slot));
  const blocked = ticked.some((m) => (state.spre[m.slot] || {}).blocking);
  const all = ticked.length === x.members.length;
  screen.append(list, el("div", { class: "actions" },
    el("button", { class: "danger", disabled: busy || !ticked.length || blocked,
                   text: all ? "PUSH ALL" : `PUSH ${ticked.length}`,
                   onclick: (ev) => requestSetPush(x, ticked, ev) }),
    el("button", { class: "ghost", text: "DELETE…", disabled: busy, onclick: (ev) => requestSetDelete(x, ev) })));
  if (!state.notice && !writing()) status(setLine(x, ticked));
}

function requestSetPush(x, ticked, opener) {
  const only = ticked.map((m) => m.slot);
  const whole = ticked.length === x.members.length;
  showConfirm({ action: "set-push", name: x.name, only: whole ? null : only, after: "set" },
    "PUSH THIS SET?", "WRITE", opener,
    el("div", {}, el("b", { text: x.name }), ` · ${ticked.length} unit${ticked.length === 1 ? "" : "s"}`),
    ...ticked.map((m) => el("div", { text: `${setSlotLabel(m.slot)}: ${x.labels[m.slot]}` })),
    el("div", { class: "warn", text: "Replaces the firmware on each unit above, one after another. A backup of each is saved first. It stops at the first failure." }),
    dim("Units not ticked are not touched. Each unit restarts into the new firmware; allow a minute or two per unit."));
}

function requestSetDelete(x, opener) {
  showConfirm({ action: "set-delete", name: x.name, after: "sets" },
    "DELETE THIS SET?", "DELETE", opener,
    el("div", {}, el("b", { text: x.name })),
    dim("Only the saved set is removed. No unit and no firmware file is touched."));
}

function requestSnapshot(opener) {
  showConfirm({ action: "set-snapshot", after: "sets" },
    "SAVE THE RIG AS A SET?", "SAVE", opener,
    el("div", { text: "Saves each unit's newest saved image as a set you can push back later." }),
    dim("Nothing is written to any unit. A unit with no saved image yet is left out."));
}

async function performSets(pend) {
  if (pend.action === "set-push") {
    await postJson("/api/sets/push", { name: pend.name, only: pend.only, confirm: pend.name });
    return `PUSH OF ${pend.name} STARTED. WATCH THE UNITS BELOW.`;
  }
  if (pend.action === "set-delete") {
    await postJson("/api/sets/delete", { name: pend.name, confirm: pend.name });
    return `SET ${pend.name} DELETED.`;
  }
  const r = await postJson("/api/sets/snapshot", {});
  const left = r.set.skipped && r.set.skipped.length ? ` (NO IMAGE FOR ${r.set.skipped.map(setSlotLabel).join(" ")})` : "";
  return `SAVED AS ${r.set.name}${left}.`;
}

// ------------------------------------------------------------------ get firmware

const GPAGE_BASE = 3;

// The footer on the GET FIRMWARE screen. The credit the hub's licence asks for lives here, short, instead of
// in a block on the screen that pushed the rows off the panel (the full text is on the admin page).
function getStatusText() {
  if (state.getOnline === false) return "OFFLINE: CONNECT THE PI TO A NETWORK WITH INTERNET TO DOWNLOAD";
  return "TAP A FIRMWARE TO DOWNLOAD IT" + (state.getAttribution ? " · LIST: THE FIRMWARE HUB, CC BY 4.0" : "");
}

async function renderGet(screen) {
  const gen = state.gen;
  if (state.getlist === null) {
    try {
      const r = await api("/api/getlist");
      state.getlist = r.items || [];
      state.getAttribution = r.attribution || "";
      state.getUnavailable = !!r.unavailable;
      state.getOnline = r.online !== false;
    } catch { state.getlist = []; state.getUnavailable = true; state.getOnline = true; }
    if (gen !== state.gen) return;
  }
  const items = state.getlist;
  const pages = Math.max(1, Math.ceil(items.length / rowsPer(GPAGE_BASE)));
  state.gpage = Math.max(0, Math.min(state.gpage, pages - 1));
  const list = el("div", { class: "list" });
  const offline = state.getOnline === false;
  if (offline) {
    // Nothing here can be downloaded, so say why and what to do instead, not a list of greyed rows.
    list.append(
      el("div", { class: "fwrow", text: "OFFLINE: NO INTERNET, SO NO DOWNLOADS. FIRMWARE ALREADY ON THIS PI CAN STILL BE FLASHED." }),
      el("div", { class: "fwrow", text: "TO ADD A FIRMWARE WITHOUT INTERNET, COPY A .FWSC FROM A USB STICK: fm1-switch import" }));
    screen.append(list, el("div", { class: "actions" },
      el("button", { class: "ghost", text: "BACK", onclick: () => { state.view = "settings"; render(); } })));
    if (!state.notice && !writing()) status(getStatusText());
    return;
  }
  if (!items.length) list.append(el("div", { class: "fwrow", text: state.getUnavailable ? "THE FIRMWARE LIST IS NOT AVAILABLE (NO CACHED COPY)" : "NOTHING NEW TO FETCH" }));
  for (const e of items.slice(state.gpage * rowsPer(GPAGE_BASE), (state.gpage + 1) * rowsPer(GPAGE_BASE))) {
    list.append(el("button", {
      class: "fwrow", disabled: writing(),
      onclick: () => getFirmware(e),
    }, el("span", { text: `${e.name} ${e.version}${e.beta ? " (beta)" : ""}${e.author ? ` · by ${e.author}` : ""}` }),
       el("span", { class: "mark unknown", text: e.stars ? `★${e.stars}` : "" })));
  }
  screen.append(list,
    el("div", { class: "actions" },
    el("button", { class: "ghost", text: "BACK", onclick: () => { state.view = "settings"; render(); } }),
    pages > 1 ? el("button", { class: "ghost", text: `‹ ${state.gpage + 1}/${pages}`, disabled: state.gpage === 0,
                               onclick: () => { state.gpage--; render(); } }) : null,
    pages > 1 ? el("button", { class: "ghost", text: "›", disabled: state.gpage >= pages - 1,
                               onclick: () => { state.gpage++; render(); } }) : null));
  if (!state.notice && !writing()) {
    // The credit the list's licence asks for goes in the footer, not on the screen: a three-line block under
    // the list pushed the rows off the panel and made some firmwares impossible to tap. The full text is on
    // the admin page.
    status(getStatusText());
  }
}

async function getFirmware(e) {
  state.busy = true;
  state.phase = "Downloading…";
  render();
  status(`DOWNLOADING ${e.name.toUpperCase()}…`);
  let result;
  try {
    await postJson("/api/download", { id: e.id });
    result = `${e.name} ${e.version} IS ON THIS PI. FLASH IT FROM A UNIT.`;
    state.getlist = null;
  } catch (err) {
    result = /offline/i.test(err.message)
      ? "OFFLINE: CANNOT DOWNLOAD. CONNECT THE PI TO INTERNET, OR FLASH WHAT IS ALREADY ON IT."
      : `DOWNLOAD FAILED: ${err.message}`;
  }
  state.busy = false;
  state.phase = "";
  state.notice = true;
  try { await loadLibrary(); } catch { /* the list is refreshed on the next load */ }
  render();
  status(result);
}

// ------------------------------------------------------------------ register a unit

// Teach the Pi which sticker a NEW unit wears (roadmap B4, audit FEAT-01). The server
// reads every download-mode unit's flash id itself, proposes the letter, and only a
// server-read id can ever become a binding. Registering is metadata only: it writes a
// line in the registry and nothing on any unit.

function openRegister() {
  state.view = "register";
  state.regscan = null;        // download mode is a state of the world: scan afresh
  state.notice = false;
  render();
}

// The id tail is what distinguishes units at a glance (three of four share a long
// prefix); an unreadable or placeholder id shows its error instead.
function regRow(u, proposed) {
  const hexId = /^[0-9a-fA-F]{8,}$/.test(u.unit_id || "");
  const id = hexId ? `id …${u.unit_id.slice(-4)}` : (u.error || "id unreadable");
  const named = u.name ? ` · ${u.name}` : "";
  if (u.slot !== null && u.slot !== undefined) {         // already bound by id
    return { main: `REGISTERED AS ${u.label}${named}`, id, go: false, bound: true };
  }
  if (proposed && proposed.unit_id === u.unit_id) {
    return { main: `REGISTER AS STICKER ${proposed.letter}${named}`, id: `id …${u.unit_id.slice(-4)}`, go: true, bound: false };
  }
  return { main: `NEW UNIT${named}`, id, go: false, bound: false };
}

async function renderRegister(screen) {
  const gen = state.gen;
  if (state.regscan === null) {
    try { state.regscan = await api("/api/register/scan"); }
    catch (err) { state.regscan = { units: [], refusal: `SCAN FAILED: ${err.message}` }; }
    if (gen !== state.gen) return;
  }
  const r = state.regscan;
  const list = el("div", { class: "list" });
  // A refusal is stated on the screen itself, not only in the footer: with rows present
  // it is the first row, so the reason is unmissable at arm's length.
  if (r.refusal) list.append(el("div", { class: "fwrow", text: r.refusal }));
  if (!r.units.length && !r.refusal) {
    list.append(el("div", { class: "fwrow", text: "NO UNIT IN DOWNLOAD MODE" }));
  }
  const proposed = r.proposed
    ? { unit_id: (r.units.find((u) => u.slot === null) || {}).unit_id,
        letter: r.proposed.letter }
    : null;
  for (const u of r.units) {
    const row = regRow(u, proposed);
    list.append(el("button", {
      class: "fwrow" + (row.go ? " selected" : ""),
      disabled: writing() || !row.go,
      onclick: row.go ? (ev) => requestBind(u, r.proposed.letter, ev) : null,
    },
      el("span", { text: row.main }), el("span", { class: "mark unknown", text: row.id })));
  }
  screen.append(list, el("div", { class: "actions" },
    el("button", { class: "ghost", text: "BACK", onclick: () => { state.view = "fleet"; render(); } }),
    el("button", { class: "ghost", text: "SCAN AGAIN", disabled: writing(),
                   onclick: () => { state.regscan = null; render(); } })));
  if (!state.pending && !state.notice) {
    status(state.regscan.refusal ? state.regscan.refusal.toUpperCase()
         : proposed ? "TAP THE NEW UNIT, THEN CONFIRM"
         : "NOTHING TO REGISTER");
  }
}

// The deliberate act. The dialog names BOTH the flash id and the letter the unit will
// wear, and it rides the shared confirm (arm window + press-origin guard), so a held
// press on REGISTER cannot complete a binding its own press did not earn. The server
// re-checks all of it and re-derives the letter itself.
function requestBind(u, letter, opener) {
  showConfirm({ action: "bind", unitId: u.unit_id, letter, label: `unit …${u.unit_id.slice(-4)}` },
    "REGISTER THIS UNIT?", "REGISTER", opener,
    el("div", {}, el("b", { text: u.unit_id }), " becomes sticker ", el("b", { text: letter })),
    u.name ? el("div", {}, `Its name (${u.name}) moves with it.`) : null,
    el("div", { text: "The Pi reads this flash id in download mode and records which physical unit wears it." }),
    dim("Writes nothing to the unit. Its letter cannot be changed here afterwards."));
}

// ------------------------------------------------------------------ wiring

// Rotating changes how many rows fit a page: start the lists from their first page again.
if (typeof matchMedia === "function") {
  matchMedia("(orientation: portrait)").addEventListener("change", () => {
    state.page = 0; state.rpage = 0; state.gpage = 0; render();
  });
}
$("#confirm-cancel").addEventListener("click", closeConfirm);
$("#confirm-go").addEventListener("pointerdown", () => { state.pressOnGo = true; });
$("#confirm-go").addEventListener("click", doWrite);
$("#chooser-cancel").addEventListener("click", closeChooser);
$("#chooser-restore").addEventListener("click", () => chooserChoose("restore"));
$("#chooser-delete").addEventListener("click", () => chooserChoose("delete"));
// BACK goes up one level on the routes screens, and to the fleet from everywhere else.
const BACK_TO = { patch: "routes", clock: "routes", routeadd: "patch", routeopt: "routeadd", pname: "routes", ptype: "pname", set: "sets" };
$("#back").addEventListener("click", () => {
  state.view = BACK_TO[state.view] || "fleet";
  if (state.view === "patch" && !routerPatch(state.rpatch)) state.view = "routes";
  if (state.view === "sets") state.sname = null;
  state.pending = null; state.notice = false; render();
});
$("#routes").addEventListener("click", () => openRoutes());
$("#sets").addEventListener("click", () => openSets());
$("#settings").addEventListener("click", () => { state.view = "settings"; state.notice = false; render(); });

// Poll the fleet so a unit entering download mode shows up without any gesture.
setInterval(() => { if (state.busy) pollPhase(); }, 1000);
// Every 2 s on every view, so another client's write is noticed wherever the operator is
// (audit §7.4). The fleet view re-renders; other views only follow the server's busy phase,
// so a half-made selection is never rebuilt under a finger. A failed poll says so (§7.5).
let pollFails = 0;
setInterval(async () => {
  if (state.pending) return;
  try {
    if (state.view === "fleet") await loadFleet(true); else await pollPhase();
    if (["routes", "patch", "clock"].includes(state.view)) await refreshRouter();
    if (["sets", "set"].includes(state.view)) await refreshSets();
    if (pollFails >= 2) status("");
    pollFails = 0;
  } catch {
    if (++pollFails === 2) status("PI SERVICE NOT RESPONDING");
  }
}, 2000);

loadFleet().catch((err) => status(`Startup problem: ${err.message}`));

// Report the viewport the browser actually gave us. cog picks this from the DRM output
// and its device-scale, which on this panel is NOT necessarily 800x480 -- and when the
// design was sized in viewport units, an unexpected viewport made everything the wrong
// size on the physical screen. The server logs it so the layout can be checked against
// facts rather than a photograph.
(function reportViewport() {
  const info = {
    vw: window.innerWidth, vh: window.innerHeight,
    dpr: window.devicePixelRatio,
    screen: `${screen.width}x${screen.height}`,
  };
  fetch("/api/viewport?" + new URLSearchParams(info)).catch(() => {});
})();

