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
      actions.append(el("button", { text: "Download", onclick: () => download(e) }));
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
    toast(`Download refused: ${err.message}`, "bad");
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

function renderHubMeta() {
  const badge = $("#hub-badge");
  if (!state.meta || !state.meta.available) {
    badge.className = "badge bad";
    badge.textContent = "hub offline";
    badge.title = (state.meta && state.meta.error) || "catalogue unavailable";
    $("#attribution").textContent = "";
    return;
  }
  const stale = state.meta.stale;
  badge.className = `badge ${stale ? "warn" : "ok"}`;
  badge.textContent = stale ? "hub cached" : "hub ok";
  badge.title = `${state.meta.count} firmwares · ${state.meta.updated}`;
  $("#search").placeholder = `Search ${state.meta.count} firmwares…`;
  $("#attribution").replaceChildren(
    document.createTextNode(state.meta.attribution + " "),
    el("span", { text: stale
      ? "Showing a cached copy — press Refresh with Wi-Fi to update."
      : "" }),
  );
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
  } catch (err) {
    toast(`Startup problem: ${err.message}`, "bad");
  }
})();
