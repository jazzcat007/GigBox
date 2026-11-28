"use strict";

// FM-1 fleet UI. Plain DOM, no framework: the whole point is that the Pi serves this
// from its own standard library, so nothing needs installing and nothing needs building.
//
// This page browses the hub, inspects packages and manages the library. It does not flash:
// writing to a unit happens only on the touch panel (/fleet), which names units by sticker
// and flash id and asks for one confirmation (audit UX-06: one meaning of "Unit A").

const state = {
  catalog: null,
  meta: null,
  library: [],
  kind: "",
  query: "",
  limit: 40,
  busy: false,
};

const $ = (sel) => document.querySelector(sel);

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined) continue;
    node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return node;
}

async function api(path, opts) {
  opts = opts || {};
  opts.headers = Object.assign({}, tokenHeaders(), opts.headers);
  const res = await fetch(path, opts);
  const text = await res.text();
  let data;
  try { data = text ? JSON.parse(text) : {}; } catch { data = { error: text }; }
  // A 401 means this browser is off-Pi and has not presented the mutation token yet
  // (audit SEC-01/02). Ask once, remember it for this browser session, retry a single
  // time; the panel on the Pi's own loopback never gets a 401.
  if (res.status === 401 && !opts._tokenRetry) {
    await askToken();
    if (tokenHeaders()["X-FM1-Token"]) {
      return api(path, Object.assign({}, opts, { _tokenRetry: true }));
    }
  }
  if (!res.ok) throw new Error(data.error || `${res.status} ${res.statusText}`);
  return data;
}

// The mutation token for a LAN surface, kept for this browser session only.
function tokenHeaders() {
  try {
    const t = sessionStorage.getItem("fm1-token");
    return t ? { "X-FM1-Token": t } : {};
  } catch { return {}; }
}

async function askToken() {
  let value = null;
  try {
    value = window.prompt(
      "FM-1 write token (printed where the server was started, and in ~/.config/fm1/token on the Pi)");
  } catch { return; }
  if (!value) return;
  try { sessionStorage.setItem("fm1-token", value.trim()); } catch { /* private mode */ }
}

let toastTimer = null;
function toast(message, kind = "") {
  document.querySelectorAll(".toast").forEach((n) => n.remove());
  const node = el("div", { class: `toast ${kind}`, text: message });
  document.body.append(node);
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => node.remove(), 6000);
}

// ------------------------------------------------------------------ modal

function openModal(title, body, actions) {
  $("#modal-title").textContent = title;
  const bodyNode = $("#modal-body");
  bodyNode.replaceChildren();
  for (const child of body) bodyNode.append(child);
  const actionNode = $("#modal-actions");
  actionNode.replaceChildren();
  for (const a of actions) {
    actionNode.append(el("button", {
      class: a.class || "",
      text: a.label,
      onclick: () => { if (a.close !== false) closeModal(); a.onclick && a.onclick(); },
    }));
  }
  $("#modal").hidden = false;
}

function closeModal() { $("#modal").hidden = true; }

$("#modal").addEventListener("click", (ev) => {
  if (ev.target.id === "modal") closeModal();
});
document.addEventListener("keydown", (ev) => {
  if (ev.key === "Escape") closeModal();
});

// ------------------------------------------------------------------ library

function renderLibrary() {
  const host = $("#library");
  host.replaceChildren();
  $("#library-hint").textContent = state.library.length
    ? `${state.library.length} package${state.library.length === 1 ? "" : "s"}`
    : "";

  if (!state.library.length) {
    host.append(el("p", { class: "note",
      text: "No packages yet. Download one from the hub below, or copy a .fwsc onto the Pi." }));
    return;
  }

  for (const p of state.library) {
    const card = el("div", { class: "card" },
      el("h3", { text: p.name }),
      el("div", { class: "row" },
        el("span", { class: "tag ident", text: p.identity || "?" }),
        el("span", { class: "tag", text: `${Math.round(p.size / 1024)} KiB` }),
        el("span", { class: `tag ${p.severity === "ok" ? "ok" : "warn"}`,
                     text: p.severity === "ok" ? "complete" : "needs installer" }),
      ),
      el("div", { class: "what", text: p.headline }),
      el("div", { class: "row" },
        p.catalog_match
          ? el("span", { class: "tag ok", text: "verified against hub" })
          : el("span", { class: "tag warn", text: "not in hub catalogue" }),
      ),
      el("div", { class: "actions" },
        // A package that needs its maker's installer stays visible, but must not offer a
        // one-tap install: that is the Melodee failure, repeated. The user asked us to
        // surface these, not hide them, so it is shown with the reason attached.
        el("button", { class: "ghost", text: "Details", onclick: () => showPackage(p) }),
      ),
    );
    host.append(card);
  }
}

function showPackage(p) {
  const body = [
    el("div", { class: `infobox`, text: p.headline }),
    el("ul", {}, p.details.map((d) => el("li", { text: d }))),
    el("div", { class: "note", text: `sha256 ${p.sha256}` }),
  ];
  openModal(p.name, body, [{ label: "Close" }]);
}

// ------------------------------------------------------------------ hub

function renderHub() {
  const host = $("#hub");
  host.replaceChildren();
  if (!state.catalog) {
    host.append(el("p", { class: "note", text: "Catalogue unavailable." }));
    return;
  }

  const q = state.query.trim().toLowerCase();
  let items = state.catalog;
  if (state.kind) items = items.filter((e) => (e.kind || []).includes(state.kind));
  if (q) {
    items = items.filter((e) =>
      e.name.toLowerCase().includes(q) ||
      (e.author || "").toLowerCase().includes(q) ||
      (e.what || "").toLowerCase().includes(q) ||
      e.id.toLowerCase().includes(q));
  }
  items = items.slice().sort((a, b) => (b.stars || 0) - (a.stars || 0));

  if (!items.length) {
    host.append(el("p", { class: "note", text: "Nothing matches." }));
    return;
  }

  // Render a page at a time. 277 cards is a 69,000-pixel page on a phone — slow to
  // paint on the Pi's worth of JavaScript, and unusable to scroll.
  const shown = items.slice(0, state.limit);

  for (const e of shown) {
    const have = state.library.find((p) => p.catalog_id === e.id);
    const tags = el("div", { class: "row" },
      el("span", { class: "tag ident", text: prettyIdent(e) }),
      el("span", { class: "tag", text: e.kind_label || "" }),
      e.beta ? el("span", { class: "tag warn", text: "beta" }) : null,
      e.chip === "new" ? el("span", { class: "tag ok", text: "saves on new chip" }) : null,
      e.chip === "old" ? el("span", { class: "tag warn", text: "older chip only" }) : null,
    );

    const actions = el("div", { class: "actions" });
    if (!e.installable_by_us) {
      actions.append(el("span", { class: "note", text: "Needs its maker's installer." }));
    } else if (have) {
      actions.append(el("span", { class: "tag ok", text: "downloaded" }));
    } else {
      actions.append(el("button", {
        text: "Download",
        disabled: isOffline() ? "" : null,
        title: isOffline() ? "Offline: connect the Pi to a network with internet to download" : null,
        onclick: () => download(e),
      }));
    }

    host.append(el("div", { class: "card" },
      el("h3", { text: e.name }),
      el("div", { class: "byline" }, `by ${e.author} · v${e.version}`,
         e.repo && /^https?:\/\//.test(e.repo)
           ? el("span", {}, " · ", el("a", { href: e.repo, target: "_blank", rel: "noopener", text: "repository" }))
           : null),
      tags,
      el("div", { class: "what", text: e.what }),
      actions,
    ));
  }

  if (items.length > state.limit) {
    host.append(el("div", { class: "row", style: "grid-column: 1 / -1; justify-content: center; padding: 8px 0;" },
      el("span", { class: "hint", text: `Showing ${state.limit} of ${items.length}` }),
      el("button", {
        text: "Show more",
        onclick: () => { state.limit += 60; renderHub(); },
      }),
    ));
  }
}

async function download(e) {
  toast(`Downloading ${e.name}…`);
  try {
    const res = await api("/api/download", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id: e.id }),
    });
    toast(`${e.name} verified and saved as ${res.name}`, "ok");
    await loadLibrary();
  } catch (err) {
    // The Pi's own words say what still works when it is offline; show them as a note, not a fault.
    toast(/offline/i.test(err.message) ? err.message : `Download refused: ${err.message}`,
          /offline/i.test(err.message) ? "" : "bad");
  }
}

// The catalogue's ident is a regex ("FM-1_95[3-9]\d") because some firmwares vary their
// last digits per version. Show it readable, and say so rather than implying the unit
// will literally report that string.
function prettyIdent(e) {
  if (!e.ident) return "";
  const simple = /^FM-1_\d+$/.test(e.ident);
  return simple ? e.ident : e.ident + " (pattern)";
}

function renderKinds() {
  const host = $("#kinds");
  host.replaceChildren();
  const kinds = [{ id: "", label: "All" }].concat(
    (state.meta && state.meta.kinds) || []);
  for (const k of kinds) {
    host.append(el("button", {
      "aria-pressed": String(state.kind === k.id),
      text: k.label,
      onclick: () => { state.kind = k.id; state.limit = 40; renderKinds(); renderHub(); },
    }));
  }
}

function showOfflineNote(text) {
  const n = $("#offline-note");
  n.textContent = text || "";
  n.hidden = !text;
}

function isOffline() { return !!(state.meta && state.meta.online === false); }

function renderHubMeta() {
  const badge = $("#hub-badge");
  const offline = isOffline();
  if (!state.meta || !state.meta.available) {
    badge.className = "badge bad";
    badge.textContent = offline ? "offline" : "hub offline";
    badge.title = (state.meta && state.meta.error) || "catalogue unavailable";
    $("#attribution").textContent = offline && state.meta ? state.meta.offline_note : "";
    showOfflineNote(offline && state.meta ? state.meta.offline_note : "");
    return;
  }
  const stale = state.meta.stale;
  badge.className = `badge ${offline || stale ? "warn" : "ok"}`;
  badge.textContent = offline ? "offline" : stale ? "hub cached" : "hub ok";
  badge.title = offline
    ? `No internet. Showing the catalogue saved ${state.meta.age_hours} h ago · ${state.meta.count} firmwares`
    : `${state.meta.count} firmwares · ${state.meta.updated}`;
  showOfflineNote(offline
    ? `Offline: showing the catalogue saved ${state.meta.age_hours} hours ago. ${state.meta.offline_note}` : "");
  $("#search").placeholder = `Search ${state.meta.count} firmwares…`;
  $("#attribution").replaceChildren(
    document.createTextNode(state.meta.attribution + " "),
    el("span", { text: offline
      ? `Offline: showing the catalogue saved ${state.meta.age_hours} hours ago. ${state.meta.offline_note}`
      : stale ? "Showing a cached copy — press Refresh with Wi-Fi to update." : "" }),
  );
}

// ---------------------------------------------------------------------------------- clock
//
// The Pi has no battery clock: with no internet its date can be wrong. When it says so AND this
// browser disagrees by more than two minutes, offer to set it from this browser's time.

async function checkClock() {
  let c;
  try { c = await api("/api/clock"); } catch { return; }
  const note = $("#clock-note");
  const skew = Date.now() / 1000 - c.now;
  if (c.trusted || Math.abs(skew) <= 120) { note.hidden = true; return; }
  const mins = Math.round(Math.abs(skew) / 60);
  const how = mins < 90 ? `${mins} minutes` : mins < 2880 ? `${Math.round(mins / 60)} hours` : `${Math.round(mins / 1440)} days`;
  note.replaceChildren(
    el("span", { text: `The Pi's clock is off by about ${how} and it has no internet to set it. ` }),
    el("button", { text: "Set it from this browser", onclick: async () => {
      try {
        const r = await api("/api/clock/set", { method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ confirm: "clock", epoch: Math.floor(Date.now() / 1000) }) });
        toast(r.message || "Clock set.", "ok");
        checkClock();
      } catch (err) { toast(`Clock not set: ${err.message}`, "bad"); }
    } }));
  note.hidden = false;
}

// ------------------------------------------------------------------ boot

async function loadState() {
  const data = await api("/api/state");
  state.meta = Object.assign({}, data.catalog, { kinds: [] });
  if (data.busy) state.busy = true;
  renderHubMeta();
}

async function loadCatalog() {
  const data = await api("/api/catalog");
  state.catalog = data.entries || [];
  state.meta = Object.assign({}, data.meta, { kinds: data.kinds });
  renderHubMeta();
  renderKinds();
  renderHub();
}

async function loadLibrary() {
  const data = await api("/api/library");
  state.library = data.items || [];
  renderLibrary();
  renderHub();
}

async function refresh() {
  toast("Refreshing…");
  try {
    // Re-reads the catalogue and the library only. Reading units is the touch panel's job
    // (one identity path, one meaning of "Unit A"): this page never talks to a device.
    await loadCatalog();
    await loadLibrary();
  } catch (err) {
    toast(`Refresh failed: ${err.message}`, "bad");
  }
}

// ------------------------------------------------------------- channel layout
//
// Which slice of the controller's 16 channels goes to which unit. The router shifts each
// slice onto the unit's own channels, so no unit needs a channel set by hand. Two layouts
// are built in (computed from each unit's channel count); a custom one is edited here.

const layout = { data: null, name: null, draft: null, unitEdits: {} };

const post = (path, body) => api(path, {
  method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });

const spanText = (first, n) => (n === 1 ? `${first}` : `${first}–${first + n - 1}`);

function numInput(value, onchange, disabled) {
  return el("input", { type: "number", min: "1", max: "16", step: "1", value, disabled: disabled ? "" : null,
    class: "chan", oninput: (ev) => onchange(Number(ev.target.value)) });
}

function currentLayout() {
  return layout.data.layouts.find((l) => l.name === layout.name) || layout.data.layouts[0];
}

function renderLayout() {
  const root = $("#layout");
  root.replaceChildren();
  const d = layout.data;
  if (!d) return;
  $("#layout-hint").textContent = `${d.source} plays; the units follow`;
  if (d.error) root.append(el("p", { class: "note", text: d.error }));
  if (!layout.name || !d.layouts.some((l) => l.name === layout.name)) {
    layout.name = d.applied && d.layouts.some((l) => l.name === d.applied) ? d.applied : d.layouts[0].name;
    layout.draft = null;
  }
  root.append(unitsCard(d), layoutCard(d));
}

function unitsCard(d) {
  const dirty = Object.keys(layout.unitEdits).length > 0;
  const rows = d.units.map((u) => {
    const e = layout.unitEdits[u.key] || {};
    const set = (field) => (v) => {
      layout.unitEdits[u.key] = Object.assign({ count: u.count, device: u.device }, layout.unitEdits[u.key], { [field]: v });
      renderLayoutBar();
    };
    return el("div", { class: "srow" },
      el("label", { text: `Unit ${u.label}` }),
      el("div", { class: "sfield" }, "uses ", numInput(e.count ?? u.count, set("count")), " channel(s), listens from channel ",
        numInput(e.device ?? u.device, set("device"))));
  });
  return el("details", { class: "sgroup", open: "" },
    el("summary", { text: "Units: how many channels each uses" }),
    el("div", { class: "hint pad", text: "A one-voice unit uses 1 channel; a four-track one uses 4. “Listens from” is the channel the unit itself is set to (normally 1): the router translates, so nothing on the unit needs changing." }),
    ...rows,
    el("div", { class: "settings-bar", id: "layout-unitbar" },
      el("button", { class: "primary", id: "layout-unitsave", text: "Save units", disabled: dirty ? null : "", onclick: saveLayoutUnits })));
}

function renderLayoutBar() {
  const b = $("#layout-unitsave");
  if (b) b.disabled = Object.keys(layout.unitEdits).length === 0;
}

async function saveLayoutUnits() {
  try {
    await post("/api/router/layouts/units", { units: layout.unitEdits });
    layout.unitEdits = {};
    layout.data = await api("/api/router/layouts");
    renderLayout();
    toast("Units saved" + (layout.data.applied ? "; the running layout was rebuilt." : "."));
  } catch (err) { toast(err.message, "bad"); }
}

function layoutCard(d) {
  const cur = currentLayout();
  const custom = !cur.builtin;
  if (!layout.draft || layout.draft.name !== cur.name) {
    layout.draft = { name: cur.name, slices: cur.slices.map((s) => ({ ...s })) };
  }
  const pick = el("select", { id: "layout-pick", onchange: (ev) => { layout.name = ev.target.value; layout.draft = null; renderLayout(); } },
    ...d.layouts.map((l) => el("option", { value: l.name, selected: l.name === cur.name ? "" : null,
      text: `${l.name}${l.builtin ? " (built in)" : ""}${l.name === d.applied ? " — running" : ""}` })));
  const rows = layout.draft.slices.map((s) => el("div", { class: "srow" },
    el("label", { text: `Unit ${s.label}` }),
    el("div", { class: "sfield" },
      "controller ch ", numInput(s.start, (v) => { s.start = v; syncLayoutSummary(); }, !custom),
      ` for ${s.count} → unit ch `, numInput(s.device, (v) => { s.device = v; syncLayoutSummary(); }, !custom),
      el("span", { class: "unit", id: `sum-${s.key}`, text: "" }))));
  const actions = el("div", { class: "settings-bar" },
    el("button", { class: "primary", text: "Apply", disabled: cur.error ? "" : null, onclick: () => applyLayout(cur.name) }),
    el("button", { class: "ghost", text: custom ? "Save changes" : "Copy to custom…", onclick: () => saveCustom(custom ? cur.name : null) }),
    custom ? el("button", { class: "ghost", text: "Delete", onclick: () => deleteCustom(cur.name) }) : null);
  const card = el("details", { class: "sgroup", open: "" },
    el("summary", { text: "Layout" }),
    el("div", { class: "srow" }, el("label", { for: "layout-pick", text: "Layout" }), el("div", { class: "sfield" }, pick)),
    cur.error ? el("div", { class: "hint pad", text: cur.error }) : null,
    cur.builtin ? el("div", { class: "hint pad", text: cur.name === "packed"
      ? "Packed: each unit takes only the channels it uses, one after another."
      : "Blocks: each unit owns a block, so a unit's channels never move when another changes." }) : null,
    ...rows, actions);
  queueMicrotask(syncLayoutSummary);
  return card;
}

function syncLayoutSummary() {
  for (const s of layout.draft ? layout.draft.slices : []) {
    const n = document.getElementById(`sum-${s.key}`);
    if (n) n.textContent = `${spanText(s.start, s.count)} → ${spanText(s.device, s.count)}`;
  }
}

function applyLayout(name) {
  openModal(`Run the ${name} layout?`, [
    el("p", { text: "The router will send each slice of the controller's channels to its unit. Nothing is written to a unit." }),
  ], [
    { label: "Cancel", class: "ghost" },
    { label: "Apply", class: "primary", onclick: async () => {
      try {
        layout.data = (await post("/api/router/layouts/apply", { name, confirm: name }));
        renderLayout();
        toast(`The ${name} layout is running.`);
      } catch (err) { toast(err.message, "bad"); }
    } },
  ]);
}

async function saveCustom(existing) {
  let name = existing;
  if (!name) {
    try { name = window.prompt("Name for the custom layout (letters, digits, space, - _ .)", "my layout"); } catch { return; }
    if (!name) return;
  }
  const slices = {};
  for (const s of layout.draft.slices) slices[s.key] = { start: s.start, count: s.count, device: s.device };
  try {
    layout.data = await post("/api/router/layouts/save", { layout: { name, source: layout.data.source, slices } });
    layout.name = name;
    layout.draft = null;
    renderLayout();
    toast(`Saved ${name}.`);
  } catch (err) { toast(err.message, "bad"); }
}

function deleteCustom(name) {
  openModal(`Delete the ${name} layout?`, [el("p", { text: "The patch it made stays on the router until you apply another layout." })], [
    { label: "Cancel", class: "ghost" },
    { label: "Delete", class: "danger", onclick: async () => {
      try {
        layout.data = await post("/api/router/layouts/delete", { name, confirm: name });
        layout.name = null;
        renderLayout();
      } catch (err) { toast(err.message, "bad"); }
    } },
  ]);
}

async function loadLayout() {
  layout.data = await api("/api/router/layouts");
  layout.unitEdits = {};
  renderLayout();
}

// ------------------------------------------------------------------ settings
//
// Generated from GET /api/settings, which comes from the schema in config.py: a new tunable
// there appears here with no change to this file. A field is sent only when it differs from
// what the server reported; "Default" sends null, which removes the key from the file.

const settings = { data: null, edits: {} };

function settingValueText(s, v) {
  if (s.kind === "bool") return v ? "yes" : "no";
  return s.unit ? `${v} ${s.unit}` : String(v);
}

function renderSettings() {
  const root = $("#settings");
  root.replaceChildren();
  const data = settings.data;
  if (!data) return;
  $("#settings-hint").textContent = data.path;
  for (const g of data.groups) {
    const items = data.settings.filter((s) => s.group === g.id);
    if (!items.length) continue;
    const rows = items.map(settingRow);
    root.append(el("details", { class: "sgroup", open: g.id !== "advanced" ? "" : null },
      el("summary", { text: g.label }), ...rows));
  }
  syncSettingsBar();
}

function settingRow(s) {
  const edited = Object.prototype.hasOwnProperty.call(settings.edits, s.key);
  const shown = edited ? settings.edits[s.key] : s.value;
  const lock = !s.editable ? s.why_locked || "Not editable here."
    : s.source === "env" ? `Fixed by the environment variable ${s.env}.` : "";
  const numeric = s.kind === "int" || s.kind === "float";
  const onValue = (val) => {
    if (val === s.value) delete settings.edits[s.key]; else settings.edits[s.key] = val;
    syncSettingsBar();
  };
  let input;
  if (s.kind === "bool") {
    input = el("input", { id: `set-${s.key}`, type: "checkbox", checked: shown ? "" : null,
      disabled: lock ? "" : null, onchange: (ev) => onValue(ev.target.checked) });
    if (shown) input.checked = true;
  } else if (s.kind === "choice") {
    input = el("select", { id: `set-${s.key}`, disabled: lock ? "" : null,
      onchange: (ev) => onValue(ev.target.value) },
      ...s.choices.map((c) => el("option", { value: c, text: c, selected: c === shown ? "" : null })));
  } else {
    input = el("input", {
      id: `set-${s.key}`,
      type: numeric ? "number" : "text",
      value: shown === null ? s.default : shown,
      min: numeric ? s.min : null,
      max: numeric ? s.max : null,
      step: s.kind === "int" ? "1" : numeric ? "any" : null,
      disabled: lock ? "" : null,
      spellcheck: "false",
      autocomplete: "off",
      oninput: (ev) => {
        const raw = ev.target.value;
        onValue(numeric ? (raw === "" ? raw : Number(raw)) : raw);
      },
    });
  }
  const badges = [];
  if (s.source === "file") badges.push(el("span", { class: "badge", text: "customised" }));
  if (s.restart) badges.push(el("span", { class: "badge warn", text: "restart to apply" }));
  if (lock) badges.push(el("span", { class: "badge", text: "locked" }));
  const reset = !lock && (s.in_file || edited) ? el("button", {
    class: "ghost small", text: "Default", title: `Back to ${settingValueText(s, s.default)}`,
    onclick: () => { settings.edits[s.key] = null; renderSettings(); },
  }) : null;
  if (edited && settings.edits[s.key] === null && s.kind !== "bool" && s.kind !== "choice") input.value = s.default;
  return el("div", { class: "srow" },
    el("label", { for: `set-${s.key}`, text: s.label }),
    el("div", { class: "sfield" }, input, s.unit ? el("span", { class: "unit", text: s.unit }) : null, reset),
    el("div", { class: "sbadges" }, ...badges),
    s.help || lock ? el("div", { class: "hint", text: [s.help, lock].filter(Boolean).join(" ") }) : null);
}

function syncSettingsBar() {
  const n = Object.keys(settings.edits).length;
  $("#settings-save").disabled = n === 0;
  $("#settings-discard").disabled = n === 0;
  $("#settings-save").textContent = n ? `Save ${n} change${n === 1 ? "" : "s"}` : "Save changes";
}

async function loadSettings() {
  settings.data = await api("/api/settings");
  settings.edits = {};
  renderSettings();
}

async function saveSettings() {
  try {
    const res = await api("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ values: settings.edits }),
    });
    settings.data = res;
    settings.edits = {};
    renderSettings();
    const n = Object.keys(res.changed || {}).length;
    const restart = (res.restart || []).map((k) => res.settings.find((s) => s.key === k).label);
    toast(n ? `Saved ${n} setting${n === 1 ? "" : "s"}.${restart.length
      ? ` Restart the service for: ${restart.join(", ")}.` : " Applied now."}` : "Nothing changed.",
      "ok");
  } catch (err) {
    toast(`Not saved: ${err.message}`, "bad");   // all or nothing: the edits stay on screen
  }
}

$("#settings-save").addEventListener("click", saveSettings);
$("#settings-discard").addEventListener("click", () => { settings.edits = {}; renderSettings(); });

$("#refresh").addEventListener("click", refresh);

let searchTimer = null;
$("#search").addEventListener("input", (ev) => {
  state.query = ev.target.value;
  state.limit = 40;
  clearTimeout(searchTimer);
  searchTimer = setTimeout(renderHub, 120);
});

(async function boot() {
  try {
    await loadState();
    await loadCatalog();
    await loadLibrary();
    await loadLayout();
    await loadSettings();
    await checkClock();
  } catch (err) {
    toast(`Startup problem: ${err.message}`, "bad");
  }
})();
