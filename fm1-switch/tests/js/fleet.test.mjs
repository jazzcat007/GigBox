// Tests for fm1_switch/web/fleet.js, run with:  node --test tests/js/
//
// fleet.js is a plain browser script with top-level side effects, so it is loaded into a
// vm context with a minimal fake DOM and a scripted fetch. No jsdom, nothing to install.

import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";
import { fileURLToPath } from "node:url";

const SRC = fs.readFileSync(
  path.join(path.dirname(fileURLToPath(import.meta.url)), "../../fm1_switch/web/fleet.js"), "utf8");

class FakeNode {
  constructor(tag) {
    this.tag = tag; this.children = []; this.attrs = {}; this.listeners = {};
    this.className = ""; this.hidden = false; this.disabled = false; this._text = "";
    this.rect = null;    // what getBoundingClientRect() reports; null = zeros (no geometry)
  }
  getBoundingClientRect() { return this.rect || { left: 0, top: 0, width: 0, height: 0 }; }
  get textContent() { return this._text + this.children.map((c) => c.textContent).join(""); }
  set textContent(v) { this._text = String(v); this.children = []; }
  append(...kids) {
    for (const k of kids) {
      // like a browser: anything that is not a Node becomes text, so a stray null is "null"
      this.children.push(k instanceof FakeNode ? k : Object.assign(new FakeNode("#text"), { _text: String(k) }));
    }
  }
  replaceChildren(...kids) { this.children = []; this.append(...kids); }
  setAttribute(k, v) { this.attrs[k] = v; }
  addEventListener(ev, fn) { (this.listeners[ev] ||= []).push(fn); }
  // Fire one listener the way an event does, with target/currentTarget set. Used for
  // pointerdown (the press-origin guard) and for clicks that must carry an opener rect.
  dispatch(type, ev = {}) {
    const e = { target: this, currentTarget: this, ...ev };
    for (const fn of this.listeners[type] || []) fn(e);
  }
  click() { if (!this.disabled) this.dispatch("click"); }
  walk(out = []) { out.push(this); this.children.forEach((c) => c.walk(out)); return out; }
  buttons() { return this.walk().filter((n) => n.tag === "button"); }
  button(text) { return this.buttons().find((b) => b.textContent === text || b.textContent.startsWith(text)); }
}

const IDS = ["screen", "status", "back", "settings", "routes", "sets", "summary", "confirm", "confirm-body",
             "confirm-cancel", "confirm-go", "confirm-title", "confirm-box", "confirm-actions",
             "chooser", "chooser-title", "chooser-body", "chooser-box", "chooser-actions",
             "chooser-cancel", "chooser-restore", "chooser-delete"];

const ROUTER = {
  router: { running: true, patch: "jam", attached: ["fm1-a", "fm1-b"], missing: ["fm1-c"], held: [] },
  active: "jam", default: "default", error: "",
  slots: ["a", "b", "c", "d"].map((x) => ({ slot: `fm1-${x}`, label: x.toUpperCase() })),
  patches: [
    { name: "jam", routes: [{ index: 0, text: "A → B", route: { source: "fm1-a", dest: "fm1-b" } },
                            { index: 1, text: "B → C · to ch 5", route: {} }] },
    { name: "solo", routes: [] },
  ],
};
const tick = () => new Promise((r) => setTimeout(r, 0));
async function settle() { for (let i = 0; i < 8; i++) await tick(); }

// routes: {"/api/fleet": data | fn(url, opts)}. Longest prefix wins. Every call is logged.
async function load({ units, packages, compat = {}, flash = () => ({ ok: true }),
                      bind = () => ({ ok: true, slot: "fm1-a", label: "A" }), build = 1 }) {
  const nodes = Object.fromEntries(IDS.map((i) => [i, new FakeNode("div")]));
  nodes.confirm.hidden = true;
  nodes.chooser.hidden = true;
  nodes.__build = build;
  const reloads = { n: 0 };
  const calls = [];
  // A controllable clock for the dialog's arm window: `elapse(ms)` moves it forward.
  let nowMs = 1_000_000;
  const elapse = (ms) => { nowMs += ms; };
  const fetch = async (url, opts = {}) => {
    calls.push({ url, opts });
    let body;
    let status = 200;
    if (url === "/api/fleet") body = { units, build: nodes.__build, busy: nodes.__busy || null,
                                      last_event: nodes.__last || null, now: nodes.__now || 0 };
    else if (url === "/api/library") body = { items: packages };
    else if (url === "/api/router") body = nodes.__router || ROUTER;
    else if (url.startsWith("/api/router/")) body = { ok: true, route: "A → B", sent: 96 };
    else if (url === "/api/sets") body = nodes.__sets || SETS;
    else if (url.startsWith("/api/sets/preflight")) body = { steps: nodes.__pre || [] };
    else if (url.startsWith("/api/sets/")) body = { ok: true, set: { name: "rig-x", skipped: [] } };
    else if (url === "/api/flashlog") body = { items: nodes.__log || [] };
    else if (url.startsWith("/api/backups")) body = { items: nodes.__backups || [] };
    else if (url === "/api/getlist") body = { items: nodes.__getlist || [], attribution: nodes.__attr || "", unavailable: !!nodes.__unavail };
    else if (url === "/api/register/scan") {
      // The default matches the server's no-unit answer; tests that need a bus set __scan.
      body = nodes.__scan ?? { units: [], unbound: 0, free: [], proposed: null,
        refusal: "no unit is in download mode — put the new unit into download mode and scan again" };
    } else if (url === "/api/register/bind") {
      try { body = await bind(JSON.parse(opts.body)); } catch (e) { body = { error: e.message }; status = 409; }
    } else if (url.startsWith("/api/fleet/package")) {
      const name = new URLSearchParams(url.split("?")[1]).get("name");
      body = { compat: compat[name] ?? null };
    } else if (url === "/api/flash") {
      try { body = await flash(JSON.parse(opts.body)); } catch (e) { body = { error: e.message }; status = 409; }
    } else body = {};
    return {
      ok: status < 400, status, statusText: "",
      text: async () => JSON.stringify(body),
    };
  };
  const ctx = vm.createContext({
    document: {
      querySelector: (sel) => nodes[sel.slice(1)],
      createElement: (t) => new FakeNode(t),
      createTextNode: (s) => Object.assign(new FakeNode("#text"), { _text: String(s) }),
    },
    // localStorage as fleet.js reads it (savedToken): nodes.__token holds "fm1-token".
    localStorage: { getItem: (k) => (k === "fm1-token" ? nodes.__token || null : null) },
    Node: FakeNode, fetch, URLSearchParams, encodeURIComponent,
    performance: { now: () => nowMs },
    setInterval: () => 0,
    window: { innerWidth: 800, innerHeight: 480, devicePixelRatio: 1 },
    screen: { width: 800, height: 480 },
    location: { reload: () => { reloads.n++; } },
    console,
  });
  vm.runInContext(SRC, ctx);
  await settle();
  return { ctx, nodes, calls, reloads, elapse, run: (code) => vm.runInContext(code, ctx) };
}

const SET_SLOTS = ["fm1-a", "fm1-b", "fm1-c", "fm1-d"].map((slot) => ({ slot, label: slot.slice(-1).toUpperCase() }));
const SETS = { slots: SET_SLOTS, library: [], error: "",
  push: { running: false, set: "", steps: [], ok: null, message: "", finished: 0 },
  sets: [{ name: "jam", members: SET_SLOTS.map((s) => ({ slot: s.slot, package: s.label + ".fwsc" })),
           labels: Object.fromEntries(SET_SLOTS.map((s) => [s.slot, s.label + ".fwsc"])) },
         { name: "pair", members: [{ slot: "fm1-b", package: "b.fwsc" }, { slot: "fm1-d", package: "d.fwsc" }],
           labels: { "fm1-b": "b.fwsc", "fm1-d": "d.fwsc" } }] };

const unit = (o = {}) => ({
  unit_id: "ABCDEF123456", label: "A", slot: "slot-a", download: true, running: false,
  present: true, firmware: "v1.2", chip: "856014", chip_kind: "old", port: "1-1", ...o,
});
const pkg = (name, o = {}) => ({ name, sha256: "sha-" + name, severity: "ok", identity: "id-" + name, ...o });
const ok = { severity: "ok", note: "fine" };
const warn = { severity: "warn", note: "will not keep saves" };

const flashCalls = (calls) => calls.filter((c) => c.url === "/api/flash");

test("fleet view: one tile per unit, tail of the flash id, port/device ids get no tail", async () => {
  const t = await load({ units: [unit(), unit({ unit_id: "port:1-2", label: "B" }),
                                 unit({ unit_id: "device:9", label: "C" })], packages: [] });
  const tiles = t.nodes.screen.buttons();
  assert.equal(tiles.length, 3);
  assert.match(tiles[0].textContent, /id 3456/);
  assert.doesNotMatch(tiles[1].textContent, /\bid\b/);
  assert.doesNotMatch(tiles[2].textContent, /\bid\b/);
  assert.equal(t.nodes.screen.className, "grid");
});

test("state text follows download > running > present > absent", async () => {
  const t = await load({
    units: [unit({ unit_id: "1", label: "A" }),
            unit({ unit_id: "2", label: "B", download: false, running: true }),
            unit({ unit_id: "3", label: "C", download: false, running: false, present: true }),
            unit({ unit_id: "4", label: "D", download: false, running: false, present: false })],
    packages: [],
  });
  const text = t.nodes.screen.buttons().map((b) => b.textContent);
  assert.match(text[0], /READY TO FLASH/);
  assert.match(text[1], /RUNNING/);
  assert.match(text[2], /SETTING UP/);
  assert.match(text[3], /NOT CONNECTED/);
  assert.equal(t.nodes.summary.textContent, "1 RUNNING · 1 READY · 1 OFF");
});

test("empty fleet says so instead of showing a blank screen", async () => {
  // Copy changed with the first-run work (FEAT-01): the empty screen is now a card that
  // guides to REGISTER, not the old dead end that pointed at a helpless SETTINGS.
  const t = await load({ units: [], packages: [] });
  assert.match(t.nodes.screen.textContent, /NO UNITS SEEN YET/);
});

test("unit view sorts ok, then unknown, then warn; ties by name", async () => {
  const packages = [pkg("z-ok.fwsc"), pkg("a-warn.fwsc"), pkg("m-unknown.fwsc"), pkg("a-ok.fwsc")];
  const compat = { "z-ok.fwsc": ok, "a-ok.fwsc": ok, "a-warn.fwsc": warn };  // m-unknown: null
  const t = await load({ units: [unit()], packages, compat });
  t.nodes.screen.buttons()[0].click();           // open unit A
  await settle();
  const rows = t.nodes.screen.walk().filter((n) => n.className.startsWith("fwrow"));
  const names = rows.map((r) => r.children[0].textContent);
  assert.deepEqual(names, ["a-ok", "z-ok"]);   // PAGE = 2: all that fits above the buttons
  t.nodes.screen.button("NEXT").click();
  await settle();
  const page2 = t.nodes.screen.walk().filter((n) => n.className.startsWith("fwrow"))
    .map((r) => r.children[0].textContent);
  assert.deepEqual(page2, ["m-unknown", "a-warn"]);
});

test("compatibility is asked per package with THIS unit's chip", async () => {
  const t = await load({ units: [unit({ chip: "ab cd" })], packages: [pkg("p.fwsc")] });
  t.nodes.screen.buttons()[0].click();
  await settle();
  const asked = t.calls.filter((c) => c.url.startsWith("/api/fleet/package"));
  assert.equal(asked.length, 1);
  assert.match(asked[0].url, /name=p\.fwsc/);
  assert.match(asked[0].url, /chip=ab%20cd/);
});

test("FLASH is disabled until a firmware is chosen and the unit is connected with a flash id", async () => {
  const t = await load({ units: [unit(), unit({ unit_id: "X2", label: "B", download: false })],
                         packages: [pkg("p.fwsc")], compat: { "p.fwsc": ok } });
  t.nodes.screen.buttons()[0].click();
  await settle();
  assert.equal(t.nodes.screen.button("FLASH").disabled, true);       // nothing selected yet
  t.nodes.screen.button("p").click();
  await settle();
  assert.equal(t.nodes.screen.button("FLASH").disabled, false);

  t.nodes.back.click();                                                  // unit B: not in download mode
  t.nodes.screen.buttons()[1].click();
  await settle();
  t.nodes.screen.button("p").click();
  await settle();
  const go = t.nodes.screen.button("NOT REGISTERED");
  assert.ok(go);
  assert.equal(go.disabled, true);
});

async function openConfirm(t, pkgName = "p.fwsc") {
  t.nodes.screen.buttons()[0].click();
  await settle();
  t.nodes.screen.button(pkgName.replace(/\.fwsc$/, "")).click();
  await settle();
  t.nodes.screen.button("FLASH").click();
  await settle();
}

// Complete the confirm dialog the way a real press does: past the arm window, with the
// press starting on the confirm button itself (pointerdown), then the click.
async function pressGo(t) {
  t.elapse(800);
  t.nodes["confirm-go"].dispatch("pointerdown");
  t.nodes["confirm-go"].click();
  await settle();
}

test("FLASH only opens the confirm dialog: nothing is written", async () => {
  const t = await load({ units: [unit()], packages: [pkg("p.fwsc")], compat: { "p.fwsc": ok } });
  await openConfirm(t);
  assert.equal(t.nodes.confirm.hidden, false);
  assert.match(t.nodes["confirm-body"].textContent, /Unit A/);
  assert.match(t.nodes["confirm-body"].textContent, /p\.fwsc/);
  assert.match(t.nodes["confirm-body"].textContent, /currently v1\.2/);
  assert.doesNotMatch(t.nodes["confirm-body"].textContent, /null/);
  assert.equal(flashCalls(t.calls).length, 0);
});

test("CANCEL closes the dialog and writes nothing", async () => {
  const t = await load({ units: [unit()], packages: [pkg("p.fwsc")], compat: { "p.fwsc": ok } });
  await openConfirm(t);
  t.nodes["confirm-cancel"].click();
  await settle();
  assert.equal(t.nodes.confirm.hidden, true);
  assert.equal(t.run("state.pending"), null);
  assert.equal(flashCalls(t.calls).length, 0);
});

test("a warn-compat firmware shows its warning in the confirm dialog", async () => {
  const t = await load({ units: [unit({ chip: "new1", chip_kind: "new" })],
                         packages: [pkg("p.fwsc")], compat: { "p.fwsc": warn } });
  await openConfirm(t);
  assert.match(t.nodes["confirm-body"].textContent, /will not keep saves/);
});

test("WRITE posts the flash id, package, sha256 and the flash id as confirm, exactly once", async () => {
  const t = await load({ units: [unit()], packages: [pkg("p.fwsc")], compat: { "p.fwsc": ok } });
  await openConfirm(t);
  await pressGo(t);
  await settle();
  const posts = flashCalls(t.calls);
  assert.equal(posts.length, 1);
  assert.equal(posts[0].opts.method, "POST");
  assert.deepEqual(JSON.parse(posts[0].opts.body),
    { unit_id: "ABCDEF123456", package: "p.fwsc", sha256: "sha-p.fwsc", confirm: "ABCDEF123456" });
  assert.match(t.nodes.status.textContent, /installed/);
  assert.equal(t.nodes.confirm.hidden, true);

  t.nodes["confirm-go"].click();                       // a second tap with no pending write
  await settle();
  assert.equal(flashCalls(t.calls).length, 1);
});

test("a click on WRITE whose press never started on it does nothing (press-origin guard)", async () => {
  const t = await load({ units: [unit()], packages: [pkg("p.fwsc")], compat: { "p.fwsc": ok } });
  await openConfirm(t);
  t.elapse(800);                                       // past the arm window
  t.nodes["confirm-go"].click();                       // no pointerdown preceded it
  await settle();
  assert.equal(flashCalls(t.calls).length, 0, "no write from a click without a press");
  assert.equal(t.nodes.confirm.hidden, false);
  await pressGo(t);                                    // the same click with a press works
  assert.equal(flashCalls(t.calls).length, 1);
});

test("a freshly opened dialog ignores clicks for its first 600 ms (arm window)", async () => {
  const t = await load({ units: [unit()], packages: [pkg("p.fwsc")], compat: { "p.fwsc": ok } });
  await openConfirm(t);
  t.nodes["confirm-go"].dispatch("pointerdown");       // even with the press ON the button
  t.nodes["confirm-go"].click();
  await settle();
  assert.equal(flashCalls(t.calls).length, 0, "no write inside the arm window");
  t.elapse(650);                                       // just past the window, once
  t.nodes["confirm-go"].dispatch("pointerdown");
  t.nodes["confirm-go"].click();
  await settle();
  assert.equal(flashCalls(t.calls).length, 1);
});

test("the repeats of the press that opened the dialog can never confirm it (UX-01)", async () => {
  // The measured case: a stationary finger taps an opener, the dialog opens under it, and
  // the resistive panel keeps repeating. Repeats arrive as clicks with no new press on
  // WRITE; some land past the arm window. None may write.
  const t = await load({ units: [unit()], packages: [pkg("p.fwsc")], compat: { "p.fwsc": ok } });
  await openConfirm(t);
  for (let i = 0; i < 5; i++) {
    t.elapse(200);                                     // 200..1000 ms after the dialog opened
    t.nodes["confirm-go"].click();
    await settle();
  }
  assert.equal(flashCalls(t.calls).length, 0, "no write from any repeat of the opener press");
  assert.equal(t.nodes.confirm.hidden, false);
});

const opener820x480 = (left, top, width, height) => ({ left, top, width, height });

test("the actions row sits at the vertical end away from the opener (audit-measured rects)", async () => {
  // Unit-view FLASH opener, audit-measured at 800x480: [12,338,263,406], bottom half.
  const t = await load({ units: [unit()], packages: [pkg("p.fwsc")], compat: { "p.fwsc": ok } });
  await t.run("state.selected = state.units[0].unit_id; state.view = 'unit'; render()");
  await settle();
  t.nodes.screen.button("p").click();
  await settle();
  const open = t.nodes.screen.button("FLASH");
  open.rect = opener820x480(12, 338, 251, 68);
  open.dispatch("click");
  await settle();
  assert.equal(t.nodes["confirm-box"].className, "modal-box actions-top",
    "a bottom-half opener puts the actions row at the top of the box");
  assert.equal(t.nodes["confirm-actions"].className, "modal-actions",
    "a bottom-left opener keeps the confirm on the right (away from it)");
});

test("a bottom-right opener moves the confirm button to the left (measured SEVERAL overlap)", async () => {
  // The SEVERAL screen's FLASH n UNITS, audit-measured [406,267,788,334]: it sat 13,366 px^2
  // under the confirm. With the row at the top and the confirm at the far left, no overlap.
  const t = await load({ units: fleetOf([C, "C"], [D, "D"]), packages: [pkg("p.fwsc")],
                         compat: { "p.fwsc": ok } });
  await t.run("state.selected = state.units[0].unit_id; state.view = 'unit'; render()");
  await settle();
  t.nodes.screen.button("p").click();
  await settle();
  t.nodes.screen.button("SEVERAL…").click();
  await settle();
  t.nodes.screen.button("· D").click();          // second target
  await settle();
  const flash2 = t.nodes.screen.button("FLASH 2 UNITS");
  flash2.rect = opener820x480(406, 267, 382, 67);
  flash2.dispatch("click");
  await settle();
  assert.equal(t.nodes["confirm-box"].className, "modal-box actions-top");
  assert.equal(t.nodes["confirm-actions"].className, "modal-actions go-left");
});

test("RESTORE's BACK UP no longer sits under its own confirm (audit UX-02)", async () => {
  // BACK UP opener, audit-measured [603,338,788,405]: 6,630 px^2 under its own "BACK UP" go.
  const t = await load({ units: fleetOf([C, "C"]), packages: [] });
  await t.run("state.selected = state.units[0].unit_id; state.view = 'restore'; render()");
  await settle();
  const bu = t.nodes.screen.button("BACK UP");
  bu.rect = opener820x480(603, 338, 185, 67);
  bu.dispatch("click");
  await settle();
  assert.equal(t.nodes["confirm-box"].className, "modal-box actions-top");
  assert.equal(t.nodes["confirm-actions"].className, "modal-actions go-left");
});

test("with no opener geometry the dialog keeps the pre-audit layout; the guards hold it", async () => {
  const t = await load({ units: [unit()], packages: [pkg("p.fwsc")], compat: { "p.fwsc": ok } });
  await openConfirm(t);          // fake-DOM clicks carry no rect
  assert.equal(t.nodes["confirm-box"].className, "modal-box");
  assert.equal(t.nodes["confirm-actions"].className, "modal-actions");
  await pressGo(t);
  assert.equal(flashCalls(t.calls).length, 1, "an armed, genuinely-pressed confirm still writes");
});

test("POSTs carry the stored X-FM1-Token when one is saved, and no header when it is not", async () => {
  // Loopback panel: no token saved, no header (the server never asks this client).
  const t = await load({ units: [unit()], packages: [pkg("p.fwsc")], compat: { "p.fwsc": ok } });
  await t.run("state.selected = state.units[0].unit_id; state.view = 'unit'; render()");
  await settle();
  t.nodes.screen.button("p").click();
  await settle();
  t.nodes.screen.button("FLASH").click();
  await settle();
  await pressGo(t);
  const bare = t.calls.find((c) => c.url === "/api/flash");
  assert.equal(bare.opts.headers["X-FM1-Token"], undefined);
  assert.equal(bare.opts.headers["Content-Type"], "application/json");

  // LAN opt-in: the operator saved the token; every mutation must present it.
  t.nodes.__token = "654321";
  t.nodes.screen.button("FLASH").click();
  await settle();
  await pressGo(t);
  const second = t.calls.filter((c) => c.url === "/api/flash")[1];
  assert.equal(second.opts.headers["X-FM1-Token"], "654321");
  assert.equal(second.opts.headers["Content-Type"], "application/json");
});

test("a refused write is reported as FAILED, not as success", async () => {
  const t = await load({
    units: [unit()], packages: [pkg("p.fwsc")], compat: { "p.fwsc": ok },
    flash: () => { throw new Error("sha256 changed"); },
  });
  await openConfirm(t);
  await pressGo(t);
  await settle();
  assert.match(t.nodes.status.textContent, /FAILED: sha256 changed/);
  assert.doesNotMatch(t.nodes.status.textContent, /installed/);
});

test("the page reports its viewport to the server", async () => {
  const t = await load({ units: [], packages: [] });
  const v = t.calls.find((c) => c.url.startsWith("/api/viewport"));
  assert.ok(v);
  assert.match(v.url, /vw=800/);
  assert.match(v.url, /vh=480/);
});

test("a RUNNING unit can be flashed; the dialog says it will be put into download mode first", async () => {
  const t = await load({ units: [unit({ download: false, running: true })],
                         packages: [pkg("p.fwsc")], compat: { "p.fwsc": ok } });
  await openConfirm(t);
  assert.match(t.nodes["confirm-body"].textContent, /download mode first/);
  await pressGo(t);
  await settle();
  assert.equal(flashCalls(t.calls).length, 1);
});

test("a unit already in download mode gets no 'download mode first' note", async () => {
  const t = await load({ units: [unit()], packages: [pkg("p.fwsc")], compat: { "p.fwsc": ok } });
  await openConfirm(t);
  assert.doesNotMatch(t.nodes["confirm-body"].textContent, /download mode first/);
});

test("units without a flash id or not connected cannot be flashed, whatever is selected", async () => {
  for (const [u, label] of [
    [unit({ unit_id: "port:1-1.2", download: false, running: true }), "NOT REGISTERED"],
    [unit({ unit_id: "device:sg0" }), "NOT REGISTERED"],
    [unit({ download: false, running: false, present: false }), "NOT CONNECTED"],
    [unit({ download: false, running: false, present: true }), "NOT READY"],
  ]) {
    const t = await load({ units: [u], packages: [pkg("p.fwsc")], compat: { "p.fwsc": ok } });
    t.nodes.screen.buttons()[0].click();
    await settle();
    t.nodes.screen.button("p").click();
    await settle();
    const b = t.nodes.screen.button(label);
    assert.ok(b, `expected a ${label} button`);
    assert.equal(b.disabled, true);
    b.click();
    await settle();
    assert.equal(t.nodes.confirm.hidden, true);
    assert.equal(flashCalls(t.calls).length, 0);
  }
});

test("while a write runs the screen is locked: FLASHING…, no firmware or back taps", async () => {
  let release;
  const gate = new Promise((r) => { release = r; });
  const t = await load({ units: [unit()], packages: [pkg("p.fwsc"), pkg("q.fwsc")],
                         compat: { "p.fwsc": ok, "q.fwsc": ok }, flash: () => gate });
  await openConfirm(t);
  await pressGo(t);
  await settle();                                   // request is in flight, not finished
  const b = t.nodes.screen.button("FLASHING");
  assert.ok(b, "button should say FLASHING…");
  assert.equal(b.disabled, true);
  assert.match(b.className, /busy/);
  assert.ok(t.nodes.screen.walk().filter((n) => n.className.startsWith("fwrow")).every((n) => n.disabled));
  assert.equal(t.nodes.back.disabled, true);
  assert.match(t.nodes.status.textContent, /DO NOT UNPLUG/);
  b.click();
  await settle();
  assert.equal(flashCalls(t.calls).length, 1);       // no second request
  release({ ok: true });
  await settle();
  assert.equal(t.run("state.busy"), false);
  assert.ok(t.nodes.screen.button("FLASH"));
  assert.match(t.nodes.status.textContent, /installed/);
});

test("the footer always names the selected firmware", async () => {
  const t = await load({ units: [unit()], packages: [pkg("a.fwsc"), pkg("b.fwsc")],
                         compat: { "a.fwsc": ok, "b.fwsc": ok } });
  t.nodes.screen.buttons()[0].click();
  await settle();
  t.nodes.screen.button("b").click();
  await settle();
  assert.match(t.nodes.status.textContent, /SELECTED: b — TAP FLASH/);
  t.nodes.screen.button("a").click();
  await settle();
  assert.match(t.nodes.status.textContent, /SELECTED: a —/);
});

test("turning the page clears the selection: FLASH cannot fire for a row that is off screen", async () => {
  const names = ["a", "b", "c", "d", "e"].map((n) => n + ".fwsc");
  const compat = Object.fromEntries(names.map((n) => [n, ok]));
  const t = await load({ units: [unit()], packages: names.map((n) => pkg(n)), compat });
  t.nodes.screen.buttons()[0].click();
  await settle();
  t.nodes.screen.button("a").click();               // page 1: a, b, c
  await settle();
  assert.equal(t.nodes.screen.button("FLASH").disabled, false);

  t.nodes.screen.button("NEXT").click();            // page 2: d, e
  await settle();
  assert.equal(t.run("state.pkg"), null);
  const go = t.nodes.screen.button("FLASH");
  assert.equal(go.disabled, true);
  go.click();
  await settle();
  assert.equal(t.nodes.confirm.hidden, true);        // no dialog for a hidden selection
  assert.doesNotMatch(t.nodes.status.textContent, /SELECTED/);

  t.nodes.screen.button("d").click();               // choose what is actually on screen
  await settle();
  t.nodes.screen.button("FLASH").click();
  await settle();
  assert.match(t.nodes["confirm-body"].textContent, /d\.fwsc/);
  assert.doesNotMatch(t.nodes["confirm-body"].textContent, /a\.fwsc/);

  t.nodes["confirm-cancel"].click();
  t.nodes.screen.button("PREV").click();            // and back again also clears it
  await settle();
  assert.equal(t.run("state.pkg"), null);
  assert.equal(t.nodes.screen.button("FLASH").disabled, true);
});

test("the busy button is grey, and redrawing for it does not re-ask the server", async () => {
  let release;
  const gate = new Promise((r) => { release = r; });
  const t = await load({ units: [unit()], packages: [pkg("p.fwsc")], compat: { "p.fwsc": ok },
                         flash: () => gate });
  await openConfirm(t);
  const before = t.calls.filter((c) => c.url.startsWith("/api/fleet/package")).length;
  await pressGo(t);
  await settle();
  const b = t.nodes.screen.button("FLASHING");
  assert.ok(b && b.disabled && /busy/.test(b.className));
  const after = t.calls.filter((c) => c.url.startsWith("/api/fleet/package")).length;
  assert.equal(after, before, "compat must come from the cache while writing");
  release({ ok: true });
  await settle();
});

test("the footer shows the server's current step while writing", async () => {
  let release;
  const gate = new Promise((r) => { release = r; });
  const t = await load({ units: [unit()], packages: [pkg("p.fwsc")], compat: { "p.fwsc": ok },
                         flash: () => gate });
  await openConfirm(t);
  await pressGo(t);
  await settle();
  assert.match(t.nodes.status.textContent, /STARTING… DO NOT UNPLUG/);
  t.nodes.__busy = "Identifying units…";
  await t.run("pollPhase()");
  assert.match(t.nodes.status.textContent, /IDENTIFYING UNITS… DO NOT UNPLUG/);
  await settle();
  assert.ok(t.nodes.screen.button("FLASHING: IDENTIFYING UNITS"), "the button names the step too");
  release({ ok: true });
  await settle();
  t.nodes.__busy = null;
});

test("the page reloads itself when a new UI build is deployed, but never mid-write", async () => {
  const t = await load({ units: [unit()], packages: [], build: 1 });
  assert.equal(t.reloads.n, 0);
  t.nodes.__build = 2;
  await t.run("loadFleet()");
  assert.equal(t.reloads.n, 1);

  const t2 = await load({ units: [unit()], packages: [], build: 1 });
  t2.run("state.pending = {unitId: 'x'}");           // a dialog is open
  t2.nodes.__build = 2;
  await t2.run("loadFleet()");
  assert.equal(t2.reloads.n, 0);
  t2.run("state.pending = null; state.busy = true"); // a write is running
  await t2.run("loadFleet()");
  assert.equal(t2.reloads.n, 0);
});

test("a unit in download mode offers RESTART UNIT, which restarts by flash id", async () => {
  const t = await load({ units: [unit({ download: true, running: false })], packages: [pkg("p.fwsc")],
                         compat: { "p.fwsc": ok } });
  await t.run("state.selected = state.units[0].unit_id; state.view = 'unit'; render()");
  await settle();
  const b = t.nodes.screen.button("RESTART UNIT");
  assert.ok(b, "restart button present");
  b.click();
  await settle();
  const call = t.calls.find((c) => c.url === "/api/restart");
  assert.ok(call, "posted to /api/restart");
  assert.equal(JSON.parse(call.opts.body).confirm, JSON.parse(call.opts.body).unit_id);
});

test("settings lists recent flashes with unit letters", async () => {
  const t = await load({ units: [unit({ unit_id: "aaaaaaaa00000001", label: "A" })], packages: [] });
  t.nodes.__log = [{ at: "2026-11-22 10:01:00", event: "done", unit_id: "aaaaaaaa00000001",
                     package: "p.fwsc", message: "installed" }];
  await t.run("state.view = 'settings'; render()");
  await settle();
  assert.match(t.nodes.screen.textContent, /RECENT ACTIVITY/);
  assert.match(t.nodes.screen.textContent, /Unit A\s+p — installed/);
});

test("RESTORE lists this unit's backups and posts the chosen one, confirmed by flash id", async () => {
  const u = unit({ unit_id: "aaaaaaaa00000003", label: "C" });
  const t = await load({ units: [u], packages: [pkg("p.fwsc")], compat: { "p.fwsc": ok } });
  t.nodes.__backups = [{ id: "d/fm1-1.bin", saved: "20261121-150141", firmware: "FM-1 901" }];
  await t.run("state.selected = state.units[0].unit_id; state.view = 'unit'; render()");
  await settle();
  t.nodes.screen.button("RESTORE…").click();
  await settle();
  assert.match(t.nodes.status.textContent, /UNIT C: 1 BACKUP /);
  t.nodes.screen.button("★ 2026-11-21 15:01  FM-1 901").click();
  assert.equal(t.nodes.confirm.hidden, false);
  assert.match(t.nodes["confirm-body"].textContent, /Replaces EVERYTHING/);
  assert.ok(!t.calls.some((c) => c.url === "/api/restore"), "nothing is sent before WRITE");
  await pressGo(t);
  await settle();
  const call = t.calls.find((c) => c.url === "/api/restore");
  assert.deepEqual(JSON.parse(call.opts.body),
    { unit_id: "aaaaaaaa00000003", backup: "d/fm1-1.bin", confirm: "aaaaaaaa00000003" });
});

const C = "aaaaaaaa00000003", D = "aaaaaaaa00000004", B = "aaaaaaaa00000002";
const fleetOf = (...u) => u.map((x, i) => unit({ unit_id: x[0], label: x[1], download: false, running: true, ...(x[2] || {}) }));
const posts = (t, url) => t.calls.filter((c) => c.url === url).map((c) => JSON.parse(c.opts.body));

async function chooseDel(t, which, rowIndex = 0) {
  t.elapse(800);                                       // past the chooser's arm window
  t.nodes.screen.buttons().filter((b) => b.textContent === "DEL")[rowIndex].click();
  await settle();
  t.elapse(800);
  t.nodes[which].click();
  await settle();
}

test("BACK UP NOW and DEL are confirmed, then post the flash id; DEL says it cannot be undone", async () => {
  const t = await load({ units: fleetOf([C, "C"]), packages: [] });
  t.nodes.__backups = [{ id: "manual/m.bin", saved: "20261121-150141", firmware: "FM-1 901" },
                       { id: "auto/a.bin", saved: "20261121-140000", firmware: "FM-1 901" }];
  await t.run("state.selected = state.units[0].unit_id; state.view = 'restore'; render()");
  await settle();
  assert.ok(t.nodes.screen.button("★ "), "a hand-made backup is starred");
  t.nodes.screen.button("BACK UP").click();
  assert.equal(t.nodes.confirm.hidden, false);
  assert.equal(posts(t, "/api/backup").length, 0, "nothing sent before the confirmation");
  await pressGo(t);
  await settle();
  assert.deepEqual(posts(t, "/api/backup"), [{ unit_id: C, confirm: C }]);
  await t.run("state.view = 'restore'; render()");
  await settle();
  await chooseDel(t, "chooser-delete", 1);
  assert.equal(t.nodes.confirm.hidden, false, "DELETE in the chooser opens the delete confirmation");
  assert.match(t.nodes["confirm-body"].textContent, /cannot be undone/);
  assert.equal(posts(t, "/api/backup/delete").length, 0, "nothing deleted before WRITE");
  await pressGo(t);
  await settle();
  assert.deepEqual(posts(t, "/api/backup/delete"), [{ unit_id: C, backup: "auto/a.bin", confirm: C }]);
});

test("DEL opens a RESTORE / DELETE chooser and never deletes by itself", async () => {
  const t = await load({ units: fleetOf([C, "C"]), packages: [] });
  t.nodes.__backups = [{ id: "auto/a.bin", saved: "20261121-140000", firmware: "FM-1 901" }];
  await t.run("state.selected = state.units[0].unit_id; state.view = 'restore'; render()");
  await settle();
  t.elapse(800);
  t.nodes.screen.buttons().find((b) => b.textContent === "DEL").click();
  await settle();
  assert.equal(t.nodes.chooser.hidden, false, "the chooser is open");
  assert.equal(t.nodes.confirm.hidden, true, "no delete confirmation yet");
  assert.match(t.nodes["chooser-body"].textContent, /FM-1 901/);
  assert.equal(posts(t, "/api/backup/delete").length, 0);
});

test("CANCEL on the chooser closes it and sends nothing", async () => {
  const t = await load({ units: fleetOf([C, "C"]), packages: [] });
  t.nodes.__backups = [{ id: "auto/a.bin", saved: "20261121-140000", firmware: "FM-1 901" }];
  await t.run("state.selected = state.units[0].unit_id; state.view = 'restore'; render()");
  await settle();
  t.elapse(800);
  t.nodes.screen.buttons().find((b) => b.textContent === "DEL").click();
  t.elapse(800);
  t.nodes["chooser-cancel"].click();
  await settle();
  assert.equal(t.nodes.chooser.hidden, true);
  assert.equal(t.nodes.confirm.hidden, true);
  assert.equal(t.calls.filter((c) => c.url.startsWith("/api/backup")).length, 1, "only the list was read");
});

test("the chooser ignores clicks inside its arm window (a repeat of the DEL press)", async () => {
  const t = await load({ units: fleetOf([C, "C"]), packages: [] });
  t.nodes.__backups = [{ id: "auto/a.bin", saved: "20261121-140000", firmware: "FM-1 901" }];
  await t.run("state.selected = state.units[0].unit_id; state.view = 'restore'; render()");
  await settle();
  t.elapse(800);
  t.nodes.screen.buttons().find((b) => b.textContent === "DEL").click();
  await settle();
  for (let i = 0; i < 3; i++) { t.elapse(150); t.nodes["chooser-delete"].click(); await settle(); }
  assert.equal(t.nodes.confirm.hidden, true, "no delete confirmation from the repeats");
  assert.equal(t.nodes.chooser.hidden, false);
});

test("RESTORE on the chooser opens the restore confirmation and posts nothing yet", async () => {
  const t = await load({ units: fleetOf([C, "C"]), packages: [] });
  t.nodes.__backups = [{ id: "auto/a.bin", saved: "20261121-140000", firmware: "FM-1 901" }];
  await t.run("state.selected = state.units[0].unit_id; state.view = 'restore'; render()");
  await settle();
  await chooseDel(t, "chooser-restore");
  assert.equal(t.nodes.chooser.hidden, true);
  assert.equal(t.nodes.confirm.hidden, false);
  assert.match(t.nodes["confirm-title"].textContent, /RESTORE THIS UNIT/);
  assert.equal(t.calls.filter((c) => c.url === "/api/restore").length, 0);
});

test("the chooser's actions row goes away from a bottom-right DEL (DELETE on the far side)", async () => {
  const t = await load({ units: fleetOf([C, "C"]), packages: [] });
  t.nodes.__backups = [{ id: "auto/a.bin", saved: "20261121-140000", firmware: "FM-1 901" }];
  await t.run("state.selected = state.units[0].unit_id; state.view = 'restore'; render()");
  await settle();
  t.elapse(800);
  const del = t.nodes.screen.buttons().find((b) => b.textContent === "DEL");
  del.rect = { left: 700, top: 360, width: 96, height: 62 };
  del.click();
  await settle();
  assert.match(t.nodes["chooser-box"].className, /actions-top/, "bottom opener: actions go to the top");
  assert.match(t.nodes["chooser-actions"].className, /go-left/, "right opener: DELETE is on the left");
});

test("a sticker that is only a guess is drawn with a tilde", async () => {
  const t = await load({ units: fleetOf([C, "C", { running: true, present: false, hint: "port" }]), packages: [] });
  await t.run("loadFleet()");
  assert.match(t.nodes.screen.textContent, /C~/);
});

test("a write that worked but could not be re-learned does not claim the unit failed to restart", async () => {
  const t = await load({ units: fleetOf([C, "C", { running: false, present: false }]), packages: [] });
  t.nodes.__now = 1000;
  t.nodes.__last = { unit_id: C, ok: true, relearned: "not-saved", at: 990 };
  await t.run("loadFleet()");
  await t.run("state.selected = state.units[0].unit_id; state.view = 'unit'; render()");
  await settle();
  assert.match(t.nodes.screen.textContent, /NOT RE-LEARNED/);
  assert.doesNotMatch(t.nodes.screen.textContent, /DIDN'T RESTART/);
});

test("a unit that did not come back after a write says so and offers a restore", async () => {
  const t = await load({ units: fleetOf([C, "C", { running: false, present: false }]), packages: [] });
  t.nodes.__now = 1000;
  t.nodes.__last = { unit_id: C, ok: true, relearned: "not-running", at: 990 };
  await t.run("loadFleet()");
  assert.match(t.nodes.screen.textContent, /DIDN'T RESTART/);
  await t.run("state.selected = state.units[0].unit_id; state.view = 'unit'; render()");
  await settle();
  assert.match(t.nodes.screen.textContent, /POWER-CYCLE IT/);
  assert.ok(t.nodes.screen.button("RESTORE…"));
  t.nodes.__now = 990 + 2000;                      // an old event explains nothing
  await t.run("loadFleet()");
  assert.doesNotMatch(t.nodes.screen.textContent, /POWER-CYCLE/);
});

test("a failed write names the failure; a running unit is never blamed", async () => {
  const t = await load({ units: fleetOf([C, "C", { running: false, present: false }], [D, "D"]), packages: [] });
  t.nodes.__now = 100;
  t.nodes.__last = { unit_id: C, ok: false, message: "write failed", at: 99, stuck: [] };
  await t.run("loadFleet()");
  assert.match(t.run("attention(state.units[0])"), /LAST WRITE FAILED: write failed/);
  assert.equal(t.run("attention(state.units[1])"), "");
});

test("names show on the card and the confirmation, and are picked, not typed", async () => {
  const t = await load({ units: fleetOf([C, "C", { name: "BASS", firmware: "sloop-2.8.4" }]),
                         packages: [pkg("p.fwsc")], compat: { "p.fwsc": ok } });
  assert.match(t.nodes.screen.textContent, /BASS · sloop-2\.8\.4/);
  await t.run("state.selected = state.units[0].unit_id; state.view = 'unit'; render()");
  await settle();
  t.nodes.screen.button("p").click();
  await settle();
  t.nodes.screen.button("FLASH").click();
  assert.match(t.nodes["confirm-body"].textContent, /Unit C \(BASS\)/);
  t.nodes["confirm-cancel"].click();
  t.nodes.screen.button("NAME…").click();
  await settle();
  t.nodes.screen.button("DRUMS").click();
  await settle();
  assert.deepEqual(posts(t, "/api/rename"), [{ unit_id: C, name: "DRUMS", confirm: C }]);
});

test("a name that is not in the list can be typed on the on-screen keyboard", async () => {
  const t = await load({ units: fleetOf([C, "C", {}]), packages: [pkg("p.fwsc")], compat: { "p.fwsc": ok } });
  await t.run("state.selected = state.units[0].unit_id; state.view = 'unit'; render()");
  await settle();
  t.nodes.screen.button("NAME…").click();
  await settle();
  t.nodes.screen.button("TYPE…").click();
  await settle();
  for (const k of ["A", "X", "1", "SPACE", "Z", "⌫", "B"]) { t.nodes.screen.button(k).click(); await settle(); }
  assert.equal(t.run("state.typed"), "AX1 B");
  t.nodes.screen.button("SAVE").click();
  await settle();
  assert.deepEqual(posts(t, "/api/rename"), [{ unit_id: C, name: "AX1 B", confirm: C }]);
});

test("the keyboard stops at the name limit", async () => {
  const t = await load({ units: fleetOf([C, "C", {}]), packages: [], compat: {} });
  await t.run("state.selected = state.units[0].unit_id; state.view = 'type'; state.typed = ''; render()");
  await settle();
  for (let i = 0; i < 15; i++) t.nodes.screen.button("A").click();
  assert.equal(t.run("state.typed").length, 12);
});

test("an update shows on the card and selects its firmware on a visible page", async () => {
  const upd = { id: "sloop", name: "SLOOP", current: "2.8.4", latest: "2.8.5", package: "sloop-2.8.5.fwsc", have: true };
  const pk = ["a", "b", "c", "d", "sloop-2.8.5"].map((n) => pkg(n + ".fwsc"));
  const t = await load({ units: fleetOf([C, "C", { firmware: "sloop-2.8.4", update: upd }]),
                         packages: pk, compat: Object.fromEntries(pk.map((p) => [p.name, ok])) });
  assert.match(t.nodes.screen.textContent, /UPDATE 2\.8\.5/);
  await t.run("state.selected = state.units[0].unit_id; state.view = 'unit'; render()");
  await settle();
  t.nodes.screen.button("UPDATE 2.8.5").click();
  await settle();
  assert.equal(t.run("state.pkg"), "sloop-2.8.5.fwsc");
  assert.ok(t.nodes.screen.button("sloop-2.8.5"), "the selected firmware is on the visible page");
  assert.equal(t.calls.filter((c) => c.url === "/api/download").length, 0, "already on the Pi: no download");
});

test("an update the Pi does not hold is downloaded first", async () => {
  const upd = { id: "sloop", name: "SLOOP", current: "2.8.4", latest: "2.8.5", package: "sloop-2.8.5.fwsc", have: false };
  const t = await load({ units: fleetOf([C, "C", { update: upd }]), packages: [pkg("sloop-2.8.5.fwsc")],
                         compat: { "sloop-2.8.5.fwsc": ok } });
  await t.run("state.selected = state.units[0].unit_id; state.view = 'unit'; render()");
  await settle();
  t.nodes.screen.button("UPDATE").click();
  await settle();
  assert.deepEqual(posts(t, "/api/download"), [{ id: "sloop" }]);
  assert.equal(t.run("state.pkg"), "sloop-2.8.5.fwsc");
});

test("GET FIRMWARE lists the hub's picks and downloads the tapped one", async () => {
  const t = await load({ units: fleetOf([C, "C"]), packages: [] });
  t.nodes.__getlist = [{ id: "sloop", name: "SLOOP", version: "2.8.5", stars: 10, beta: false }];
  await t.run("state.view = 'settings'; render()");
  await settle();
  t.nodes.screen.button("GET FIRMWARE…").click();
  await settle();
  t.nodes.screen.button("SLOOP 2.8.5").click();
  await settle();
  assert.deepEqual(posts(t, "/api/download"), [{ id: "sloop" }]);
  assert.match(t.nodes.status.textContent, /SLOOP 2\.8\.5 IS ON THIS PI/);
});

test("SEVERAL flashes the picked units one after another and stops at the first failure", async () => {
  let n = 0;
  const t = await load({
    units: fleetOf([C, "C"], [D, "D"], [B, "B"]), packages: [pkg("p.fwsc")], compat: { "p.fwsc": ok },
    flash: (body) => { n++; if (body.unit_id === D) throw new Error("no download mode"); return { ok: true }; },
  });
  await t.run("state.selected = state.units[0].unit_id; state.view = 'unit'; render()");
  await settle();
  assert.ok(!t.nodes.screen.button("SEVERAL…"), "needs a firmware chosen first");
  t.nodes.screen.button("p").click();
  await settle();
  t.nodes.screen.button("SEVERAL…").click();
  await settle();
  t.nodes.screen.button("· D").click();
  await settle();
  t.nodes.screen.button("· B").click();
  await settle();
  t.nodes.screen.button("FLASH 3 UNITS").click();
  await settle();
  assert.equal(flashCalls(t.calls).length, 0, "nothing sent before the confirmation");
  assert.match(t.nodes["confirm-body"].textContent, /one after another/i);
  await pressGo(t);
  await settle();
  assert.deepEqual(flashCalls(t.calls).map((c) => JSON.parse(c.opts.body).unit_id), [C, D]);
  assert.equal(n, 2, "B was never attempted");
  assert.match(t.nodes.status.textContent, /C ✓ +D ✗ +B –\. D FAILED: no download mode/);
});

// ------------------------------------------------------------------ REGISTER (FEAT-01)

// The scan answers the server shapes; ids here are deliberately fake 16-hex strings --
// the bench's real ids stay out of every fixture.
const scanOf = (o = {}) => ({ units: [], unbound: 0, free: ["A", "B", "C", "D"],
                              proposed: null, refusal: null, ...o });
const scanUnit = (o = {}) => ({ unit_id: "aaaaaaaa00000001", label: "?", slot: null, name: null,
                                chip: "0x856014", firmware: "FM-1 953", port: "1-1.2",
                                device: "/dev/sg0", error: null, provisional: false,
                                running: false, present: true, ...o });
const U2 = "aaaaaaaa00000002";
const bindCalls = (calls) => calls.filter((c) => c.url === "/api/register/bind");

test("the register scan lists every download-mode unit and marks the registered ones", async () => {
  const t = await load({ units: [], packages: [] });
  t.nodes.__scan = scanOf({
    units: [scanUnit({ unit_id: U2, label: "B", slot: "fm1-b", name: "BASS" }), scanUnit()],
    unbound: 1, proposed: { slot: "fm1-a", letter: "A" },
  });
  await t.run("openRegister()");
  await settle();
  const rows = t.nodes.screen.buttons();
  assert.match(rows[0].textContent, /REGISTERED AS B/);
  assert.match(rows[0].textContent, /BASS/);
  assert.match(rows[0].textContent, /id …0002/);
  assert.equal(rows[0].disabled, true);
  assert.match(rows[1].textContent, /REGISTER AS STICKER A/);
  assert.match(rows[1].textContent, /id …0001/);
  assert.equal(rows[1].disabled, false);
  assert.match(t.nodes.status.textContent, /TAP THE NEW UNIT/);
  assert.ok(t.calls.some((c) => c.url === "/api/register/scan"), "opening REGISTER scans");
  t.nodes.screen.button("SCAN AGAIN").click();
  await settle();
  assert.equal(t.calls.filter((c) => c.url === "/api/register/scan").length, 2, "re-scan on demand");
});

test("every REGISTER refusal renders with its stated reason", async () => {
  const cases = [
    [scanOf({ refusal: "no unit is in download mode — put the new unit into download mode and scan again" }),
     /no unit is in download mode/i, /PUT THE NEW UNIT INTO DOWNLOAD MODE/],
    [scanOf({ units: [scanUnit(), scanUnit({ unit_id: U2 })], unbound: 2,
              refusal: "2 units are in download mode without a sticker — leave only one connected and scan again" }),
     /2 units are in download mode without a sticker/i, /LEAVE ONLY ONE CONNECTED/],
    [scanOf({ units: [scanUnit({ unit_id: U2, label: "B", slot: "fm1-b" })], unbound: 0,
              refusal: "every unit in download mode already has a sticker; use rename to change a unit's name" }),
     /already has a sticker/i, /USE RENAME/],
    [scanOf({ units: [scanUnit()], unbound: 1,
              refusal: "every letter (A-D) already has a unit — moving a binding is not possible from the panel yet" }),
     /every letter \(A-D\) already has a unit/i, /MOVING A BINDING IS NOT POSSIBLE/],
  ];
  for (const [scan, rowRe, statusRe] of cases) {
    const t = await load({ units: [], packages: [] });
    t.nodes.__scan = scan;
    await t.run("openRegister()");
    await settle();
    assert.match(t.nodes.screen.textContent, rowRe);
    assert.match(t.nodes.status.textContent, statusRe);
    // Nothing is bindable while the refusal stands.
    for (const b of t.nodes.screen.buttons()) {
      if (/SCAN AGAIN|BACK/.test(b.textContent)) continue;
      assert.equal(b.disabled, true);
    }
  }
});

test("REGISTER names the id and the letter; a held press cannot complete the binding", async () => {
  const t = await load({ units: [], packages: [] });
  t.nodes.__scan = scanOf({ units: [scanUnit({ unit_id: "aaaaaaaa00000001" })], unbound: 1,
                            proposed: { slot: "fm1-a", letter: "A" } });
  await t.run("openRegister()");
  await settle();
  t.nodes.screen.button("REGISTER AS STICKER A").click();
  await settle();
  assert.equal(t.nodes.confirm.hidden, false);
  const body = t.nodes["confirm-body"].textContent;
  assert.match(body, /aaaaaaaa00000001/, "the confirm names the flash id");
  assert.match(body, /sticker A/, "the confirm names the letter");
  assert.match(body, /Writes nothing to the unit/, "registering is metadata only");
  // The repeats of the press that opened the dialog: bare clicks, some landing past the
  // arm window -- none may complete a binding its own press did not earn (same guards
  // as WRITE, audit UX-01 pattern).
  for (let i = 0; i < 5; i++) {
    t.elapse(200);
    t.nodes["confirm-go"].click();
    await settle();
  }
  assert.equal(bindCalls(t.calls).length, 0, "no binding from any repeat of the opener press");
  assert.equal(t.nodes.confirm.hidden, false);
  // The deliberate act: past the arm window, press started on REGISTER itself.
  await pressGo(t);
  await settle();
  const posts = bindCalls(t.calls);
  assert.equal(posts.length, 1);
  assert.deepEqual(JSON.parse(posts[0].opts.body),
    { unit_id: "aaaaaaaa00000001", confirm: "aaaaaaaa00000001" });
  assert.match(t.nodes.status.textContent, /sticker A/);
});

test("a refused bind is reported as FAILED with the server's reason", async () => {
  const t = await load({ units: [], packages: [], bind: () => {
    throw new Error("that unit is not in download mode anymore — it left between the scan and the confirm. Scan again and confirm once more.");
  } });
  t.nodes.__scan = scanOf({ units: [scanUnit()], unbound: 1,
                            proposed: { slot: "fm1-a", letter: "A" } });
  await t.run("openRegister()");
  await settle();
  t.nodes.screen.button("REGISTER AS STICKER A").click();
  await settle();
  await pressGo(t);
  await settle();
  assert.match(t.nodes.status.textContent, /FAILED: that unit is not in download mode anymore/);
  assert.doesNotMatch(t.nodes.status.textContent, /is now sticker/);
});

test("an empty fleet offers REGISTER, which scans, and reports the register view", async () => {
  const t = await load({ units: [], packages: [] });
  assert.match(t.nodes.screen.textContent, /NO UNITS SEEN YET/);
  const b = t.nodes.screen.button("REGISTER A UNIT");
  assert.ok(b, "the first-run card carries the REGISTER action");
  assert.equal(t.nodes.summary.textContent, "SETUP");
  t.nodes.__scan = scanOf({ units: [scanUnit()], unbound: 1,
                            proposed: { slot: "fm1-a", letter: "A" } });
  b.click();
  await settle();
  assert.ok(t.calls.some((c) => c.url === "/api/register/scan"), "register scans the bus");
  assert.ok(t.nodes.screen.button("REGISTER AS STICKER A"));
});

test("SETTINGS offers REGISTER next to GET FIRMWARE, and bind events show in the activity list", async () => {
  const t = await load({ units: [], packages: [] });
  t.nodes.__log = [{ at: "2026-11-23 10:00:00", event: "bind", unit_id: "aaaaaaaa00000001",
                     message: "A" }];
  await t.run("state.view = 'settings'; render()");
  await settle();
  assert.match(t.nodes.screen.textContent, /registered as A/);
  assert.ok(t.nodes.screen.button("REGISTER…"), "REGISTER is reachable from SETTINGS");
  t.nodes.__scan = scanOf({ units: [scanUnit()], unbound: 1,
                            proposed: { slot: "fm1-a", letter: "A" } });
  t.nodes.screen.button("REGISTER…").click();
  await settle();
  assert.ok(t.calls.some((c) => c.url === "/api/register/scan"));
});

test("GET FIRMWARE names each maker and shows the hub's attribution; unreachable differs from empty", async () => {
  const t = await load({ units: fleetOf([C, "C"]), packages: [] });
  t.nodes.__getlist = [{ id: "sloop", name: "SLOOP", version: "2.8.5", stars: 1, beta: false, author: "Ada" }];
  t.nodes.__attr = "Firmware list: The Firmware Hub";
  await t.run("state.view = 'settings'; render()");
  await settle();
  t.nodes.screen.button("GET FIRMWARE…").click();
  await settle();
  assert.ok(t.nodes.screen.button("SLOOP 2.8.5 · by Ada"));
  assert.match(t.nodes.screen.textContent, /The Firmware Hub/);
  t.nodes.__getlist = []; t.nodes.__unavail = true;
  await t.run("state.getlist = null; render()");
  await settle();
  assert.match(t.nodes.screen.textContent, /NOT AVAILABLE/);
});

test("two renders in flight leave one set of backup rows (JS-01)", async () => {
  const u = unit({ unit_id: "aaaaaaaa00000003", label: "C" });
  const t = await load({ units: [u], packages: [pkg("p.fwsc")], compat: { "p.fwsc": ok } });
  t.nodes.__backups = [{ id: "d/a.bin", saved: "20261121-150141", firmware: "x" },
                       { id: "d/b.bin", saved: "20261121-150142", firmware: "y" }];
  await t.run("state.selected = state.units[0].unit_id; state.view = 'restore'; state.rpage = 0; render(); render()");
  await settle();
  const rows = t.nodes.screen.walk().filter((n) => n.className.startsWith("pair"));
  assert.equal(rows.length, 2);
});

// ------------------------------------------------------------------ setup unfinished (two-step firmware)

const pending = { package: "melodee-1.6.1.fwsc", sha256: "ab", since: 1 };

test("a unit with an unfinished setup is never shown as running or ready", async () => {
  const t = await load({ units: [unit({ download: false, running: true, setup_pending: pending })],
                         packages: [] });
  const tile = t.nodes.screen.buttons()[0];
  assert.match(tile.textContent, /SETUP UNFINISHED/);
  assert.doesNotMatch(tile.textContent, /RUNNING|READY/);
  assert.match(t.nodes.summary.textContent, /1 UNFINISHED/);
});

test("the unit screen says what the second step is, and offers FINISH SETUP", async () => {
  const t = await load({ units: [unit({ download: false, running: true, setup_pending: pending })],
                         packages: [] });
  t.nodes.screen.buttons()[0].click();
  await settle();
  const detail = t.nodes.screen.walk().map((n) => n.textContent).join(" ");
  assert.match(detail, /SETUP UNFINISHED: melodee-1\.6\.1 needs its maker's installer step/);
  assert.ok(t.nodes.screen.button("FINISH SETUP"));
});

test("FINISH SETUP posts a confirm for this unit and writes nothing", async () => {
  const t = await load({ units: [unit({ download: false, running: true, setup_pending: pending })],
                         packages: [] });
  t.nodes.screen.buttons()[0].click();
  await settle();
  t.nodes.screen.button("FINISH SETUP").click();
  await settle();
  assert.equal(t.nodes.confirm.hidden, false);
  assert.match(t.nodes["confirm-body"].textContent, /Nothing is written to the unit/);
  await pressGo(t);
  const finish = t.calls.filter((c) => c.url === "/api/setup/finish");
  assert.equal(finish.length, 1);
  assert.deepEqual(JSON.parse(finish[0].opts.body), { unit_id: "ABCDEF123456", confirm: "ABCDEF123456" });
  assert.equal(flashCalls(t.calls).length, 0);
});

const jiantPending = { package: "jiant-V10.7.81+de2922f2.fwsc", sha256: "ab", since: 1 };

test("INSTALL FROM THE PI is offered only when the server says the Pi can do it", async () => {
  const can = await load({ units: [unit({ download: false, running: true, setup_pending: jiantPending,
                                          setup_from_pi: true })], packages: [] });
  can.nodes.screen.buttons()[0].click();
  await settle();
  assert.ok(can.nodes.screen.button("INSTALL FROM THE PI"));
  assert.ok(can.nodes.screen.button("FINISH SETUP"));
  const text = can.nodes.screen.walk().map((n) => n.textContent).join(" ");
  assert.match(text, /Press INSTALL FROM THE PI/);

  const cannot = await load({ units: [unit({ download: false, running: true, setup_pending: pending,
                                             setup_from_pi: false })], packages: [] });
  cannot.nodes.screen.buttons()[0].click();
  await settle();
  assert.equal(cannot.nodes.screen.button("INSTALL FROM THE PI"), undefined);
  assert.match(cannot.nodes.screen.walk().map((n) => n.textContent).join(" "), /Finish it on a laptop/);
});

test("INSTALL FROM THE PI asks first, then posts one install for this unit", async () => {
  const t = await load({ units: [unit({ download: false, running: true, setup_pending: jiantPending,
                                        setup_from_pi: true })], packages: [] });
  t.nodes.screen.buttons()[0].click();
  await settle();
  t.nodes.screen.button("INSTALL FROM THE PI").click();
  await settle();
  assert.equal(t.nodes.confirm.hidden, false);
  assert.match(t.nodes["confirm-body"].textContent, /Saves a backup/);
  assert.equal(t.calls.filter((c) => c.url === "/api/setup/install").length, 0);
  await pressGo(t);
  const calls = t.calls.filter((c) => c.url === "/api/setup/install");
  assert.equal(calls.length, 1);
  assert.deepEqual(JSON.parse(calls[0].opts.body), { unit_id: "ABCDEF123456", confirm: "ABCDEF123456" });
  assert.equal(flashCalls(t.calls).length, 0);
});

test("flashing a two-step package says there are two steps", async () => {
  const t = await load({ units: [unit()], packages: [pkg("melodee-1.6.1.fwsc", { severity: "warn" })],
                         compat: { "melodee-1.6.1.fwsc": ok } });
  await openConfirm(t, "melodee-1.6.1.fwsc");
  assert.match(t.nodes["confirm-body"].textContent, /TWO STEPS/);
  assert.match(t.nodes["confirm-body"].textContent, /FINISH SETUP/);
});

test("an ordinary firmware written over an unfinished setup says it replaces it", async () => {
  const t = await load({ units: [unit({ download: true, running: false, setup_pending: pending })],
                         packages: [pkg("felucca-1.8.2.fwsc")], compat: { "felucca-1.8.2.fwsc": ok } });
  await openConfirm(t, "felucca-1.8.2.fwsc");
  assert.match(t.nodes["confirm-body"].textContent, /SETUP UNFINISHED for melodee-1\.6\.1/);
  assert.match(t.nodes["confirm-body"].textContent, /This write replaces it/);
  assert.doesNotMatch(t.nodes["confirm-body"].textContent, /TWO STEPS/);
});


// ------------------------------------------------------------------ routes (dawless hub)

const routerCalls = (t) => t.calls.filter((c) => c.url.startsWith("/api/router/"));
const lastBody = (c) => JSON.parse(c.opts.body);

async function openRoutes(t) {
  t.nodes.routes.click();
  await settle();
}

test("the ROUTES button is on the fleet screen only, and opens the patches with the router's state", async () => {
  const t = await load({ units: [unit()], packages: [] });
  assert.equal(t.nodes.routes.hidden, false);
  await openRoutes(t);
  assert.equal(t.nodes.routes.hidden, true);
  const text = t.nodes.screen.textContent;
  assert.match(text, /ROUTER RUNNING: JAM/);
  assert.match(text, /LIVE A B/);
  assert.match(text, /NOT FOUND C/);
  assert.match(text, /● jam/);                 // the patch the router runs is marked
  assert.match(text, /2 ROUTES/);
  assert.equal(routerCalls(t).length, 0, "looking changes nothing");
});

test("a router that is not running is said plainly, and a broken patch file is shown", async () => {
  const t = await load({ units: [unit()], packages: [] });
  t.nodes.__router = { ...ROUTER, router: { running: false, patch: null, attached: [], missing: [], held: [] } };
  await openRoutes(t);
  assert.match(t.nodes.screen.textContent, /ROUTER NOT RUNNING/);
  t.nodes.__router = { ...ROUTER, error: "x cannot be read" };
  t.nodes.back.click();
  await openRoutes(t);
  assert.match(t.nodes.screen.textContent, /PATCH FILE PROBLEM: X CANNOT BE READ/);
});

test("BACK climbs one level at a time: route editor, patch, patch list, fleet", async () => {
  const t = await load({ units: [unit()], packages: [] });
  await openRoutes(t);
  t.nodes.screen.button("● jam").click();
  assert.match(t.nodes.screen.textContent, /A → B/);
  t.nodes.screen.button("ADD ROUTE").click();
  t.nodes.screen.button("OPTIONS").click();
  assert.match(t.nodes.screen.textContent, /TRANSPOSE/);
  t.nodes.back.click(); assert.equal(t.run("state.view"), "routeadd");
  t.nodes.back.click(); assert.equal(t.run("state.view"), "patch");
  t.nodes.back.click(); assert.equal(t.run("state.view"), "routes");
  t.nodes.back.click(); assert.equal(t.run("state.view"), "fleet");
});

test("making a route: both units must be picked, a unit cannot feed itself, options are sent", async () => {
  const t = await load({ units: [unit()], packages: [] });
  await openRoutes(t);
  t.nodes.screen.button("NEW PATCH").click();
  t.nodes.screen.button("DRONE").click();
  const add = () => t.nodes.screen.button("ADD ROUTE");
  assert.equal(add().disabled, true);
  t.nodes.screen.button("A").click();            // FROM A
  assert.equal(add().disabled, true);
  const to = t.nodes.screen.buttons().filter((b) => b.textContent === "A");
  assert.equal(to[1].disabled, true, "A cannot be both ends");
  t.nodes.screen.buttons().filter((b) => b.textContent === "C")[1].click();   // TO C
  t.nodes.screen.button("OPTIONS").click();
  t.nodes.screen.buttons().filter((b) => b.textContent === "+")[0].click();   // channel 1
  t.nodes.screen.buttons().filter((b) => b.textContent === "+")[0].click();   // channel 2
  t.nodes.screen.buttons().filter((b) => b.textContent === "+")[1].click();   // +12
  t.nodes.screen.button("· BLOCK CLOCK").click();
  assert.match(t.nodes.screen.textContent, /SEND ON CHANNEL: 2/);
  assert.match(t.nodes.screen.textContent, /TRANSPOSE: \+12/);
  // the server now holds the new patch (the fake does not remember saves)
  t.nodes.__router = { ...ROUTER, patches: [...ROUTER.patches,
    { name: "DRONE", routes: [{ index: 0, text: "A → C · to ch 2 · +12 · no clock", route: {} }] }] };
  t.nodes.screen.button("ADD ROUTE").click();
  await settle();
  const [call] = routerCalls(t);
  assert.equal(call.url, "/api/router/route");
  assert.deepEqual(lastBody(call), { patch: "DRONE", route: {
    source: "fm1-a", dest: "fm1-c", remap_to: 2, transpose: 12, block: ["clock"] } });
  assert.equal(t.run("state.view"), "patch");
  assert.match(t.nodes.screen.textContent, /A → C · to ch 2 · \+12 · no clock/);
});

test("a refused route says why and stays on the editor", async () => {
  const t = await load({ units: [unit()], packages: [] });
  await openRoutes(t);
  t.nodes.screen.button("NEW PATCH").click();
  t.nodes.screen.button("JAM").click();
  t.nodes.screen.button("A").click();
  t.nodes.screen.buttons().filter((b) => b.textContent === "B")[1].click();
  const real = t.ctx.fetch;
  t.ctx.fetch = async (url, opts) => url === "/api/router/route"
    ? { ok: false, status: 409, statusText: "", text: async () => JSON.stringify({ error: "patch file unreadable" }) }
    : real(url, opts);
  t.nodes.screen.button("ADD ROUTE").click();
  await settle();
  assert.equal(t.run("state.view"), "routeadd");
  assert.match(t.nodes.status.textContent, /NOT ADDED: patch file unreadable/);
});

test("RUN THIS, STOP, DELETE, remove-route and PANIC each wait for a confirmation", async () => {
  const t = await load({ units: [unit()], packages: [] });
  await openRoutes(t);
  t.nodes.screen.button("PANIC").click();
  assert.equal(t.nodes.confirm.hidden, false);
  assert.equal(routerCalls(t).length, 0);
  await pressGoRoute(t);
  assert.deepEqual(routerCalls(t).map((c) => [c.url, lastBody(c)]), [["/api/router/panic", { confirm: "panic" }]]);
  assert.match(t.nodes.status.textContent, /SOUND OFF SENT \(96 MESSAGES\)/);

  t.nodes.screen.button("solo").click();
  t.nodes.screen.button("RUN THIS").click();
  assert.equal(t.nodes.screen.button("RUN THIS").disabled, true, "an empty patch cannot be run");
});

async function pressGoRoute(t) {
  t.elapse(1000);
  t.nodes["confirm-go"].dispatch("pointerdown");
  t.nodes["confirm-go"].click();
  await settle();
}

test("running a patch and stopping routing send the chosen name, after a confirmation", async () => {
  const t = await load({ units: [unit()], packages: [] });
  t.nodes.__router = { ...ROUTER, active: null, default: "default",
                       router: { running: false, patch: null, attached: [], missing: [], held: [] } };
  await openRoutes(t);
  t.nodes.screen.button("jam").click();
  t.nodes.screen.button("RUN THIS").click();
  assert.match(t.nodes["confirm-body"].textContent, /connects the units/);
  await pressGoRoute(t);
  const call = routerCalls(t).at(-1);
  assert.deepEqual([call.url, lastBody(call)], ["/api/router/activate", { name: "jam", confirm: "jam" }]);

  t.nodes.__router = { ...ROUTER };               // now it is the running one
  t.nodes.back.click();
  await openRoutes(t);
  t.nodes.screen.button("● jam").click();
  t.nodes.screen.button("STOP ROUTING").click();
  await pressGoRoute(t);
  const stop = routerCalls(t).at(-1);
  assert.deepEqual(lastBody(stop), { name: "", confirm: "stop" });
});

test("deleting a patch or one route names them, and needs the arm window and a press on the button", async () => {
  const t = await load({ units: [unit()], packages: [] });
  await openRoutes(t);
  t.nodes.screen.button("● jam").click();
  t.nodes.screen.button("DEL").click();
  assert.match(t.nodes["confirm-body"].textContent, /jam · A → B/);
  t.nodes["confirm-go"].dispatch("pointerdown");
  t.nodes["confirm-go"].click();                   // too soon: still inside the arm window
  await settle();
  assert.equal(routerCalls(t).length, 0);
  await pressGoRoute(t);
  const gone = routerCalls(t)[0];
  assert.deepEqual([gone.url, lastBody(gone)], ["/api/router/route/delete", { patch: "jam", index: 0, confirm: "jam" }]);

  t.nodes.screen.button("DELETE…").click();
  await pressGoRoute(t);
  const del = routerCalls(t).at(-1);
  assert.deepEqual([del.url, lastBody(del)], ["/api/router/patch/delete", { name: "jam", confirm: "jam" }]);
});

test("the routes pages never touch the flash endpoints", async () => {
  const t = await load({ units: [unit()], packages: [] });
  await openRoutes(t);
  t.nodes.screen.button("PANIC").click();
  await pressGoRoute(t);
  assert.equal(flashCalls(t.calls).length, 0);
  assert.equal(t.calls.filter((c) => /restore|backup|register|setup|restart/.test(c.url)).length, 0);
});


// ------------------------------------------------------------------ rig sets

const setCalls = (t) => t.calls.filter((c) => c.url.startsWith("/api/sets/") && !c.url.startsWith("/api/sets/preflight"));

test("SETS opens the saved sets, and a set opens with every unit ticked", async () => {
  const t = await load({ units: [unit()], packages: [] });
  assert.equal(t.nodes.sets.hidden, false);
  t.nodes.sets.click();
  await settle();
  assert.equal(t.run("state.view"), "sets");
  assert.match(t.nodes.screen.textContent, /jam/);
  assert.match(t.nodes.screen.textContent, /pair/);
  t.nodes.screen.button("jam").click();
  await settle();
  assert.equal(t.run("state.view"), "set");
  assert.ok(t.nodes.screen.button("PUSH ALL"));
  assert.equal(setCalls(t).length, 0);      // looking writes nothing
});

test("PUSH ALL waits for a confirmation, then sends the set name and no subset", async () => {
  const t = await load({ units: [unit()], packages: [] });
  t.nodes.sets.click(); await settle();
  t.nodes.screen.button("jam").click(); await settle();
  t.nodes.screen.button("PUSH ALL").click();
  assert.equal(t.nodes.confirm.hidden, false);
  assert.equal(setCalls(t).length, 0);
  await pressGoRoute(t);
  const [c] = setCalls(t);
  assert.equal(c.url, "/api/sets/push");
  assert.deepEqual(lastBody(c), { name: "jam", only: null, confirm: "jam" });
});

test("unticking a unit pushes only the rest", async () => {
  const t = await load({ units: [unit()], packages: [] });
  t.nodes.sets.click(); await settle();
  t.nodes.screen.button("jam").click(); await settle();
  t.nodes.screen.buttons().filter((b) => /^● C/.test(b.textContent))[0].click();
  assert.ok(t.nodes.screen.button("PUSH 3"));
  t.nodes.screen.button("PUSH 3").click();
  await pressGoRoute(t);
  assert.deepEqual(lastBody(setCalls(t)[0]), { name: "jam", only: ["fm1-a", "fm1-b", "fm1-d"], confirm: "jam" });
});

test("a blocked unit disables the push until it is unticked", async () => {
  const t = await load({ units: [unit()], packages: [] });
  t.nodes.__pre = [{ slot: "fm1-b", blocking: "b.fwsc is not in the library", warning: "" }];
  t.nodes.sets.click(); await settle();
  t.nodes.screen.button("pair").click(); await settle();
  assert.match(t.nodes.screen.textContent, /BLOCKED/);
  assert.match(t.nodes.status.textContent, /B BLOCKED: B\.FWSC IS NOT IN THE LIBRARY/);
  assert.equal(t.nodes.screen.button("PUSH ALL").disabled, true);
  t.nodes.screen.buttons().filter((b) => /^● B/.test(b.textContent))[0].click();
  assert.equal(t.nodes.screen.button("PUSH 1").disabled, false);
});

test("SNAPSHOT and DELETE each wait for a confirmation", async () => {
  const t = await load({ units: [unit()], packages: [] });
  t.nodes.sets.click(); await settle();
  t.nodes.screen.button("SNAPSHOT").click();
  assert.equal(t.nodes.confirm.hidden, false);
  assert.equal(setCalls(t).length, 0);
  await pressGoRoute(t);
  assert.equal(setCalls(t)[0].url, "/api/sets/snapshot");
  t.nodes.screen.button("pair").click(); await settle();
  t.nodes.screen.button("DELETE…").click();
  await pressGoRoute(t);
  assert.deepEqual(lastBody(setCalls(t)[1]), { name: "pair", confirm: "pair" });
});

test("while a push runs the set screen is locked and shows each unit's progress", async () => {
  const t = await load({ units: [unit()], packages: [] });
  t.nodes.__sets = { ...SETS, push: { running: true, set: "pair", ok: null, message: "", finished: 0,
    steps: [{ slot: "fm1-b", label: "b.fwsc", status: "done", message: "installed" },
            { slot: "fm1-d", label: "d.fwsc", status: "writing", message: "" }] } };
  t.nodes.sets.click(); await settle();
  t.nodes.screen.button("pair").click(); await settle();
  assert.match(t.nodes.status.textContent, /PUSHING: WRITING D/);
  assert.match(t.nodes.screen.textContent, /DONE/);
  assert.match(t.nodes.screen.textContent, /WRITING/);
  assert.equal(t.nodes.screen.button("PUSH ALL").disabled, true);
});
