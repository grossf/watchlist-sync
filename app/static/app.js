const $ = (sel, el = document) => el.querySelector(sel);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const ic = (name, cls = "") => `<svg class="i ${cls}" aria-hidden="true"><use href="#i-${name}"/></svg>`;
const COLORS = { in_seerr: "var(--green)", requested: "var(--blue)", would_request: "var(--purple)", skipped: "var(--grey)", not_found: "var(--amber)", errors: "var(--red)" };

let status = null;
let route = { page: "watchlists", params: new URLSearchParams() };
let editing = false;
let editOrder = [];
let confirmRemove = null;
let wasRunning = false;
let logsLoadedAt = 0;
let seerrUsers = null;
let settingsCache = null;

/* ---------- helpers ---------- */

function toast(msg) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.add("show");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => t.classList.remove("show"), 3500);
}

async function api(path, { method = "GET", json } = {}) {
  const opts = { method };
  if (json !== undefined) {
    opts.headers = { "Content-Type": "application/json" };
    opts.body = JSON.stringify(json);
  }
  const r = await fetch(path, opts);
  if (r.status === 401 && path !== "/api/login") {
    location.href = "/login";
    throw new Error("Login required");
  }
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(typeof body.detail === "string" ? body.detail : "Something went wrong");
  return body;
}

function when(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  const now = new Date();
  const time = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  const day = (x) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
  const diff = Math.round((day(now) - day(d)) / 86400000);
  if (diff === 0) return `today ${time}`;
  if (diff === 1) return `yesterday ${time}`;
  return `${d.toLocaleDateString([], { day: "numeric", month: "short" })} ${time}`;
}

function ago(iso) {
  if (!iso) return "";
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return when(iso);
}

const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;
const seerrOk = () => !!status?.seerr.check?.ok;
const isLive = (w) => status?.running && status.current?.username === w.username;

function logsLink(params) {
  return "#/logs?" + new URLSearchParams(params).toString();
}

/* ---------- routing ---------- */

function parseRoute() {
  const [path, query] = location.hash.replace(/^#\/?/, "").split("?");
  const page = ["watchlists", "connections", "logs", "settings"].includes(path) ? path : "watchlists";
  route = { page, params: new URLSearchParams(query || "") };
}

window.addEventListener("hashchange", () => {
  parseRoute();
  editing = false;
  confirmRemove = null;
  $("#sidebar").classList.remove("open");
  renderPage();
});

/* ---------- shell ---------- */

function renderShell() {
  if (!status) return;
  document.querySelectorAll("#nav a").forEach((a) => a.classList.toggle("active", a.dataset.page === route.page));
  const wlProblem = status.watchlists.some((w) => (w.check && !w.check.ok) || w.last_run?.status === "failed" || w.last_run?.errors);
  const connProblem = !status.seerr.configured || (status.seerr.check && !status.seerr.check.ok);
  $("#dot-watchlists").classList.toggle("hidden", !wlProblem);
  $("#dot-connections").classList.toggle("hidden", !connProblem);

  let text, cls = "";
  if (status.running) {
    text = `Syncing ${status.current?.username || ""}…`;
    cls = "running";
  } else if (!status.seerr.configured || !status.watchlists.length) text = "Not set up yet";
  else if (!status.schedule_enabled) text = "Idle · daily sync off";
  else text = `Idle · next sync ${status.schedule_time}`;
  const foot = $("#foot-state");
  foot.className = "row " + cls;
  foot.innerHTML = `<span class="dot"></span><span>${esc(text)}</span>`;
  $("#version").textContent = `· v${status.version}`;
  $("#github").href = status.github_url;
  $("#logout").classList.toggle("hidden", !status.password_enabled);
}

$("#menu").addEventListener("click", () => $("#sidebar").classList.toggle("open"));
$("#logout").addEventListener("click", async (e) => {
  e.preventDefault();
  await api("/api/logout", { method: "POST" });
  location.href = "/login";
});

/* ---------- watchlists ---------- */

function segments(run, live) {
  const src = live || run;
  if (!src) return null;
  const scanned = src.scanned || 0;
  const total = Math.max(src.total || 0, scanned);
  const dry = !!src.dry_run;
  const parts = [
    ["in_seerr", src.in_seerr],
    [dry ? "would_request" : "requested", src.requested],
    ["skipped", src.skipped],
    ["not_found", src.not_found],
    ["errors", src.errors],
  ].filter(([, n]) => n > 0);
  return { parts, scanned, total: total || 1, dry, notChecked: Math.max(0, total - scanned) };
}

function barHtml(seg) {
  if (!seg) return `<div class="bar"></div>`;
  return `<div class="bar">${seg.parts.map(([k, n]) => `<div style="width:${(n / seg.total) * 100}%;background:${COLORS[k]}"></div>`).join("")}</div>`;
}

function legendHtml(w, seg, run) {
  const label = { in_seerr: "in Seerr", requested: "requested", would_request: "would request", skipped: "skipped", not_found: "not found", errors: "failed" };
  const kindFor = { requested: "requested", would_request: "requested", skipped: "skipped", not_found: "not_found", errors: "error" };
  const items = seg.parts.map(([k, n]) => {
    const inner = `<span class="sw" style="background:${COLORS[k]}"></span>${n} ${label[k]}`;
    return kindFor[k] ? `<a class="lg" href="${logsLink({ watchlist: w.username, kind: kindFor[k] })}">${inner}</a>` : `<span class="lg">${inner}</span>`;
  });
  if (run?.status === "failed" && seg.notChecked) {
    items.push(`<span class="lg"><span class="sw" style="background:var(--line);box-shadow:inset 0 0 0 1px var(--line-strong)"></span>${seg.notChecked} not checked</span>`);
  }
  if (!items.length) items.push(`<span class="lg">Nothing to check</span>`);
  return items.join("");
}

function cardHtml(w) {
  const check = w.check;
  const missing = check && check.missing;
  const live = isLive(w) ? status.current : null;
  const run = w.last_run;
  const seg = missing ? null : segments(run, live);
  const count = check?.count ?? run?.total;
  const busy = status.running;
  const canSync = seerrOk() && !busy;

  const name = editing
    ? `<span class="name">${esc(w.username)}</span>`
    : `<a class="name" href="${esc(w.url)}" target="_blank" rel="noopener" title="Open on Letterboxd">${esc(w.username)}${ic("ext", "sm ext")}</a>`;
  const sub = missing ? "watchlist not found" : count != null ? plural(count, "film") : "";

  let actions;
  if (editing) {
    actions = `
      <button class="ghost" data-act="up" data-u="${esc(w.username)}" title="Move up" aria-label="Move up">${ic("up")}</button>
      <button class="ghost" data-act="down" data-u="${esc(w.username)}" title="Move down" aria-label="Move down">${ic("down")}</button>
      <button class="ghost del" data-act="remove" data-u="${esc(w.username)}" title="Remove watchlist" aria-label="Remove watchlist">${ic("trash")}</button>`;
  } else if (missing) {
    actions = `<button class="ghost" data-act="recheck" title="Check again" aria-label="Check again">${ic("rotate")}</button>`;
  } else {
    const title = !seerrOk() ? "Seerr isn't reachable" : busy && !live ? "Another sync is running" : live ? "Syncing…" : run?.status === "failed" ? "Retry sync" : "Sync now";
    actions = `<button class="ghost" data-act="sync" data-key="${esc(w.key)}" title="${title}" aria-label="${title}" ${canSync ? "" : "disabled"}>${ic("rotate", live ? "spin" : "")}</button>`;
  }

  let body = "";
  if (missing) {
    body = `<div class="err">${ic("alert", "sm")}<span>${esc(check.detail)}</span></div>`;
  } else {
    body += barHtml(seg);
    let left, right = "";
    if (live?.waiting) {
      left = `<span style="color:var(--amber)">Letterboxd asked to slow down, waiting 30 s… ${live.scanned}${live.total ? ` of ${live.total}` : ""} checked</span>`;
      right = "now";
    } else if (live) {
      left = `<span>Syncing… ${live.scanned}${live.total ? ` of ${live.total}` : ""} checked</span>`;
      right = "now";
    } else if (run && seg) {
      left = legendHtml(w, seg, run);
      right = when(run.started_at);
    } else {
      left = `<span>Not synced yet${status.schedule_enabled ? ` · syncs at ${status.schedule_time}` : ""}</span>`;
    }
    body += `<div class="row spread" style="align-items:flex-start;flex-wrap:nowrap"><div class="legend">${left}</div><span class="small dim" style="flex:none">${right}</span></div>`;
    if (!live && run?.message) {
      body += `<div class="err">${ic("alert", "sm")}<span>${esc(run.message)}${run.errors ? ` <a href="${logsLink({ watchlist: w.username, kind: "error" })}" style="text-decoration:underline;text-underline-offset:3px">Show in log</a>` : ""}</span></div>`;
    } else if (check && !check.ok) {
      body += `<div class="err warn">${ic("alert", "sm")}<span>${esc(check.detail)}</span></div>`;
    }
  }

  const confirm = confirmRemove === w.username ? `
    <div class="confirm"><span class="muted grow">Remove ${esc(w.username)}? Films already requested stay in Seerr.</span>
      <button data-act="cancel-remove">Cancel</button><button class="danger" data-act="confirm-remove" data-u="${esc(w.username)}">Remove</button></div>` : "";

  return `<div class="card wl" data-u="${esc(w.username)}" ${editing ? 'draggable="true"' : ""}>
    <div class="card-head">
      <div class="row" style="gap:6px;min-width:0">${editing ? `<span class="grip">${ic("grip")}</span>` : ""}${name}<span class="muted">${sub ? "· " + esc(sub) : ""}</span></div>
      <div class="actions">${actions}</div>
    </div>${body}${confirm}</div>`;
}

function guideHtml() {
  const seerrDone = seerrOk();
  return `<div class="card" style="padding:18px 20px">
    <h2>Get started</h2>
    <p class="muted small" style="margin:4px 0 10px">Two steps and your watchlist syncs every night.</p>
    <div class="step"><span class="num ${seerrDone ? "done" : ""}">${seerrDone ? ic("check", "sm") : "1"}</span>
      <div class="grow"><div style="font-weight:500">Connect Seerr</div><div class="small muted">${seerrDone ? `Connected as ${esc(status.seerr.check.user)}` : "URL and API key of your Seerr instance."}</div></div>
      ${seerrDone ? "" : `<a class="btn" href="#/connections">Connect</a>`}</div>
    <div class="step"><span class="num">2</span>
      <div class="grow"><div style="font-weight:500">Add your first watchlist</div><div class="small muted">Your Letterboxd username. The watchlist must be public.</div></div>
      <button data-act="add">${ic("plus", "sm")}Add</button></div>
  </div>`;
}

function renderWatchlists() {
  const s = status;
  const list = editing ? editOrder.map((u) => s.watchlists.find((w) => w.username === u)).filter(Boolean) : s.watchlists;
  const canSync = seerrOk() && !s.running && s.watchlists.length;
  const head = `<div class="page-head"><h1>Watchlists</h1><div class="row" style="gap:6px">
    ${editing
      ? `<button class="primary" data-act="done">${ic("check", "sm")}Done</button>`
      : `<button class="icon" data-act="add" title="Add watchlist" aria-label="Add watchlist">${ic("plus")}</button>
         ${s.watchlists.length ? `<button class="icon" data-act="edit" title="Reorder and remove" aria-label="Edit">${ic("pencil")}</button>
         <button class="icon" data-act="sync-all" title="${seerrOk() ? "Sync all watchlists" : "Seerr isn't reachable"}" aria-label="Sync all" ${canSync ? "" : "disabled"}>${ic("sync", s.running ? "spin" : "")}</button>` : ""}`}
  </div></div>`;

  let banners = "";
  if (s.seerr.configured && s.seerr.check && !s.seerr.check.ok) {
    banners += `<div class="banner red">${ic("alert", "sm")}<span>Can't reach Seerr at ${esc(s.seerr.url)}, so syncing is paused. <a href="#/connections">Check connections</a></span></div>`;
  }
  if (s.dry_run && s.watchlists.length) {
    banners += `<div class="banner amber">${ic("alert", "sm")}<span>Dry run is on: syncs only log what they would request. <a href="#/settings">Turn it off in Settings</a></span></div>`;
  }
  const hint = editing ? `<p class="small muted" style="margin:-6px 0 12px">Drag cards to reorder. Watchlists sync in this order.</p>` : "";
  const guide = !s.watchlists.length || !s.seerr.configured ? guideHtml() : "";
  const html = head + banners + guide + hint + `<div id="cards" class="${editing ? "editing" : ""}">${list.map(cardHtml).join("")}</div>`;
  if (html !== renderWatchlists.last || !$("#cards")) {
    $("#page").innerHTML = html;
    renderWatchlists.last = html;
  }
}

async function startSync(source) {
  try {
    await api("/api/run", { method: "POST", json: { source } });
    toast(status.dry_run ? "Dry run started" : "Sync started");
    await refresh();
  } catch (e) { toast(e.message); }
}

async function saveOrder() {
  const changed = editOrder.join() !== status.watchlists.map((w) => w.username).join();
  editing = false;
  confirmRemove = null;
  if (changed) {
    try {
      await api("/api/watchlists/order", { method: "PUT", json: { usernames: editOrder } });
      toast("Order saved");
    } catch (e) { toast(e.message); }
  }
  await refresh();
}

function move(u, delta) {
  const i = editOrder.indexOf(u);
  const j = i + delta;
  if (i < 0 || j < 0 || j >= editOrder.length) return;
  [editOrder[i], editOrder[j]] = [editOrder[j], editOrder[i]];
  renderWatchlists();
}

$("#page").addEventListener("click", async (e) => {
  const el = e.target.closest("[data-act]");
  if (!el || route.page !== "watchlists") return;
  const act = el.dataset.act;
  if (act === "add") openAddDialog();
  else if (act === "edit") { editing = true; editOrder = status.watchlists.map((w) => w.username); renderWatchlists(); }
  else if (act === "done") saveOrder();
  else if (act === "sync-all") startSync("all");
  else if (act === "sync") startSync(el.dataset.key);
  else if (act === "up") move(el.dataset.u, -1);
  else if (act === "down") move(el.dataset.u, 1);
  else if (act === "remove") { confirmRemove = el.dataset.u; renderWatchlists(); }
  else if (act === "cancel-remove") { confirmRemove = null; renderWatchlists(); }
  else if (act === "confirm-remove") {
    try {
      await api(`/api/watchlists/${encodeURIComponent(el.dataset.u)}`, { method: "DELETE" });
      editOrder = editOrder.filter((u) => u !== el.dataset.u);
      confirmRemove = null;
      toast(`Removed ${el.dataset.u}`);
      await refresh();
      if (!status.watchlists.length) editing = false;
      renderWatchlists();
    } catch (err) { toast(err.message); }
  } else if (act === "recheck") {
    el.disabled = true;
    try { status = await api("/api/checks/refresh", { method: "POST" }); render(); } catch (err) { toast(err.message); }
  }
});

let dragged = null;
$("#page").addEventListener("dragstart", (e) => {
  const card = e.target.closest?.(".wl");
  if (!editing || !card) return;
  dragged = card.dataset.u;
  card.classList.add("dragging");
  e.dataTransfer.effectAllowed = "move";
});
$("#page").addEventListener("dragend", () => { dragged = null; if (editing) renderWatchlists(); });
$("#page").addEventListener("dragover", (e) => {
  if (!dragged) return;
  const over = e.target.closest(".wl");
  e.preventDefault();
  if (!over || over.dataset.u === dragged) return;
  const rect = over.getBoundingClientRect();
  const after = e.clientY > rect.top + rect.height / 2;
  const from = editOrder.indexOf(dragged);
  editOrder.splice(from, 1);
  let to = editOrder.indexOf(over.dataset.u) + (after ? 1 : 0);
  editOrder.splice(to, 0, dragged);
  const cards = $("#cards");
  const node = cards.querySelector(`[data-u="${CSS.escape(dragged)}"]`);
  cards.insertBefore(node, after ? over.nextSibling : over);
});

/* ---------- add dialog ---------- */

function openAddDialog() {
  let checked = null;
  const root = $("#dialog");
  root.innerHTML = `<div class="overlay" id="ov"><div class="dialog" role="dialog" aria-modal="true" aria-labelledby="dlg-title">
    <div class="row spread" style="margin-bottom:14px"><h2 id="dlg-title" style="font-size:15px">Add watchlist</h2>
      <button class="ghost" id="dlg-x" aria-label="Close">${ic("x")}</button></div>
    <label class="field" for="dlg-u" style="margin-top:0">Letterboxd username or profile link</label>
    <div class="row" style="flex-wrap:nowrap"><input id="dlg-u" placeholder="letterboxd.com/yourname" autocomplete="off"><button id="dlg-check">Check</button></div>
    <div id="dlg-res"></div>
    <p class="hint">The watchlist has to be public.</p>
    <div class="card-actions"><button id="dlg-cancel">Cancel</button><button class="primary" id="dlg-add" disabled>Add watchlist</button></div>
  </div></div>`;
  const input = $("#dlg-u"), res = $("#dlg-res"), add = $("#dlg-add");
  const close = () => { root.innerHTML = ""; document.removeEventListener("keydown", onKey); };
  const onKey = (e) => { if (e.key === "Escape") close(); };
  document.addEventListener("keydown", onKey);
  const reset = () => { checked = null; res.innerHTML = ""; add.disabled = true; };

  async function check() {
    const value = input.value.trim();
    if (!value) { res.innerHTML = `<div class="result bad">${ic("alert", "sm")}<span>Enter a username first</span></div>`; return; }
    res.innerHTML = `<div class="row small muted" style="margin-top:12px">${ic("loader", "sm spin")}Checking Letterboxd…</div>`;
    $("#dlg-check").disabled = true;
    try {
      const r = await api("/api/test/watchlist", { method: "POST", json: { username: value } });
      if (r.already_added) {
        res.innerHTML = `<div class="result info">${ic("alert", "sm")}<span>${esc(r.username)} is already in your list.</span></div>`;
      } else if (r.ok) {
        checked = r.username;
        res.innerHTML = `<div class="result ok">${ic("check", "sm")}<span><b>${esc(r.display_name)}</b> (${esc(r.username)})${r.count != null ? ` · ${plural(r.count, "film")} on the watchlist` : ""}</span></div>`;
        add.disabled = false;
        add.focus();
      } else {
        res.innerHTML = `<div class="result bad">${ic("alert", "sm")}<span>${esc(r.detail)}</span></div>`;
      }
    } catch (e) {
      res.innerHTML = `<div class="result bad">${ic("alert", "sm")}<span>${esc(e.message)}</span></div>`;
    }
    $("#dlg-check").disabled = false;
  }

  input.addEventListener("input", reset);
  input.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); checked ? add.click() : check(); } });
  $("#dlg-check").addEventListener("click", check);
  $("#dlg-x").addEventListener("click", close);
  $("#dlg-cancel").addEventListener("click", close);
  $("#ov").addEventListener("mousedown", (e) => { if (e.target.id === "ov") close(); });
  add.addEventListener("click", async () => {
    if (!checked) return;
    add.disabled = true;
    try {
      await api("/api/watchlists", { method: "POST", json: { username: checked } });
      toast(`Added ${checked}`);
      close();
      await refresh();
    } catch (e) {
      res.innerHTML = `<div class="result bad">${ic("alert", "sm")}<span>${esc(e.message)}</span></div>`;
    }
  });
  input.focus();
}

/* ---------- connections ---------- */

function seerrStatusText() {
  const s = status.seerr;
  if (!s.configured) return ["Not set up", ""];
  if (!s.check) return ["Checking…", ""];
  if (s.check.ok) return [`Connected as ${s.check.user}${s.check.version ? ` · v${s.check.version}` : ""} · checked ${ago(s.check.checked_at)}`, "ok"];
  return [s.check.detail, "bad"];
}

function letterboxdStatus() {
  const lists = status.watchlists;
  if (!lists.length) return [null, "Add a watchlist to check the connection. No account needed, public pages only."];
  const checks = lists.map((w) => w.check).filter(Boolean);
  if (!checks.length) return [null, "Checking…"];
  const bad = lists.filter((w) => w.check && !w.check.ok);
  const latest = checks.map((c) => c.checked_at).sort().pop();
  if (!bad.length) return [true, `Reachable · ${plural(checks.length, "watchlist")} checked ${ago(latest)}. No account needed, public pages only.`];
  return [false, bad.map((w) => `${w.username}: ${w.check.detail}`).join(" ")];
}

function updateConnectionsStatus() {
  if (route.page !== "connections" || !$("#seerr-status")) return;
  const [text, state] = seerrStatusText();
  const el = $("#seerr-status");
  el.textContent = text;
  el.className = "status-line" + (state === "bad" ? " bad" : "");
  $("#seerr-dot").className = "dot " + (state === "ok" ? "green" : state === "bad" ? "red" : "");
  const [lbOk, lbText] = letterboxdStatus();
  $("#lb-dot").className = "dot " + (lbOk === true ? "green" : lbOk === false ? "red" : "");
  $("#lb-status").textContent = lbText;
}

async function renderConnections() {
  $("#page").innerHTML = `<div class="page-head"><h1>Connections</h1></div>
    <form class="card" id="seerr-form" autocomplete="off">
      <div class="card-head"><div class="row" style="gap:8px"><span class="dot" id="seerr-dot"></span><h2>Seerr</h2></div><span class="status-line" id="seerr-status"></span></div>
      <label class="field" for="seerr_url">URL</label>
      <input id="seerr_url" name="seerr_url" placeholder="http://192.168.1.10:5055">
      <p class="hint">An address this container can reach. localhost means the container itself.</p>
      <label class="field" for="seerr_api_key">API key</label>
      <input id="seerr_api_key" name="seerr_api_key" type="password" autocomplete="new-password">
      <label class="field" for="seerr_user_id">Request as</label>
      <div class="row" style="flex-wrap:nowrap"><select id="seerr_user_id" name="seerr_user_id"></select><button type="button" id="load-users">Load users</button></div>
      <label class="check"><input type="checkbox" name="request_4k"><span>Request 4K versions</span></label>
      <div id="seerr-result"></div>
      <div class="card-actions"><button type="button" id="test-seerr">Test connection</button><button type="submit" class="primary">Save</button></div>
    </form>
    <div class="card">
      <div class="card-head"><div class="row" style="gap:8px"><span class="dot" id="lb-dot"></span><h2>Letterboxd</h2></div>
        <button class="ghost" id="lb-recheck" title="Check again" aria-label="Check again">${ic("rotate")}</button></div>
      <p class="small muted" id="lb-status" style="margin-top:8px"></p>
    </div>`;
  updateConnectionsStatus();
  const form = $("#seerr-form");
  try { settingsCache = await api("/api/settings"); } catch (e) { toast(e.message); return; }
  form.seerr_url.value = settingsCache.seerr_url;
  form.seerr_api_key.placeholder = settingsCache.has_api_key ? "Saved. Leave empty to keep it" : "Seerr → Settings → General → API Key";
  form.request_4k.checked = settingsCache.request_4k;
  renderUserSelect();
  if (settingsCache.has_api_key && settingsCache.seerr_url && !seerrUsers) loadUsers(true);

  const body = () => ({ seerr_url: form.seerr_url.value.trim() || null, seerr_api_key: form.seerr_api_key.value.trim() || null });
  const result = (cls, text) => { $("#seerr-result").innerHTML = text ? `<div class="result ${cls}">${ic(cls === "ok" ? "check" : "alert", "sm")}<span>${esc(text)}</span></div>` : ""; };

  async function loadUsers(quiet) {
    try {
      seerrUsers = await api("/api/seerr/users", { method: "POST", json: body() });
      renderUserSelect();
      if (!quiet) toast(`Loaded ${plural(seerrUsers.length, "user")}`);
    } catch (e) { if (!quiet) result("bad", e.message); }
  }
  $("#load-users").addEventListener("click", () => loadUsers(false));
  $("#test-seerr").addEventListener("click", async (e) => {
    e.target.disabled = true;
    result("info", "Testing…");
    try {
      const r = await api("/api/test/seerr", { method: "POST", json: body() });
      result(r.ok ? "ok" : "bad", r.ok ? `Connected as ${r.user}${r.version ? ` · Seerr v${r.version}` : ""}` : r.detail);
      if (r.ok) loadUsers(true);
    } catch (err) { result("bad", err.message); }
    e.target.disabled = false;
  });
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const payload = {
      seerr_url: form.seerr_url.value,
      seerr_user_id: form.seerr_user_id.value ? Number(form.seerr_user_id.value) : null,
      request_4k: form.request_4k.checked,
    };
    if (form.seerr_api_key.value.trim()) payload.seerr_api_key = form.seerr_api_key.value.trim();
    try {
      settingsCache = await api("/api/settings", { method: "PUT", json: payload });
      form.seerr_api_key.value = "";
      form.seerr_api_key.placeholder = settingsCache.has_api_key ? "Saved. Leave empty to keep it" : "";
      result("", "");
      toast("Saved");
      await refresh();
    } catch (err) { result("bad", err.message); }
  });
  $("#lb-recheck").addEventListener("click", async (e) => {
    e.currentTarget.disabled = true;
    $("#lb-status").textContent = "Checking…";
    try { status = await api("/api/checks/refresh", { method: "POST" }); render(); } catch (err) { toast(err.message); }
  });
}

function renderUserSelect() {
  const select = $("#seerr_user_id");
  if (!select) return;
  const current = select.value || (settingsCache?.seerr_user_id ? String(settingsCache.seerr_user_id) : "");
  const users = seerrUsers || [];
  let options = `<option value="">Owner of the API key</option>` + users.map((u) => `<option value="${u.id}">${esc(u.name)} (#${u.id})</option>`).join("");
  if (current && !users.some((u) => String(u.id) === current)) options += `<option value="${esc(current)}">User #${esc(current)}</option>`;
  select.innerHTML = options;
  select.value = current;
}

/* ---------- logs ---------- */

const KIND = {
  requested: ["Requested", "b-requested"],
  would_request: ["Would request", "b-would_request"],
  not_found: ["Not found", "b-not_found"],
  skipped: ["Skipped", "b-skipped"],
  error: ["Error", "b-error"],
};

function renderLogs() {
  const kind = route.params.get("kind") || "";
  const watchlist = route.params.get("watchlist") || "";
  const chips = [["", "All"], ["not_found", "Not found"], ["requested", "Requested"], ["skipped", "Skipped"], ["error", "Errors"]];
  const names = [...new Set([...status.watchlists.map((w) => w.username), watchlist].filter(Boolean))];
  $("#page").innerHTML = `<div class="page-head"><h1>Logs</h1>
      <select id="log-list" style="width:auto"><option value="">All watchlists</option>${names.map((u) => `<option value="${esc(u)}" ${u === watchlist ? "selected" : ""}>${esc(u)}</option>`).join("")}</select></div>
    <div class="chips">${chips.map(([k, t]) => `<button class="chip ${k === kind ? "on" : ""}" data-kind="${k}">${t}</button>`).join("")}</div>
    <div class="card" style="padding:4px 16px" id="events"><div class="empty">Loading…</div></div>
    <div class="section-title">Run history</div>
    <div class="card" style="padding:4px 16px" id="runs"><div class="empty">Loading…</div></div>`;
  $("#log-list").addEventListener("change", (e) => setLogParams({ watchlist: e.target.value, kind }));
  document.querySelectorAll(".chip").forEach((c) => c.addEventListener("click", () => setLogParams({ watchlist, kind: c.dataset.kind })));
  loadLogs();
}

function setLogParams(p) {
  const params = Object.fromEntries(Object.entries(p).filter(([, v]) => v));
  location.hash = Object.keys(params).length ? logsLink(params) : "#/logs";
}

async function loadLogs() {
  logsLoadedAt = Date.now();
  const kind = route.params.get("kind") || "";
  const watchlist = route.params.get("watchlist") || "";
  const q = new URLSearchParams({ limit: "300" });
  if (kind) q.set("kind", kind);
  if (watchlist) q.set("watchlist", watchlist);
  const rq = new URLSearchParams({ limit: "30" });
  if (watchlist) rq.set("watchlist", watchlist);
  try {
    const [events, runs] = await Promise.all([api(`/api/events?${q}`), api(`/api/runs?${rq}`)]);
    if (route.page !== "logs") return;
    $("#events").innerHTML = events.length ? events.map((e) => {
      const [label, cls] = KIND[e.kind] || [e.kind, "b-skipped"];
      const title = e.slug ? `<a class="t" href="https://letterboxd.com/film/${esc(e.slug)}/" target="_blank" rel="noopener">${esc(e.title)}</a>` : `<span class="t">Sync</span>`;
      const tmdb = e.tmdb_id ? ` <a class="dim" href="https://www.themoviedb.org/movie/${e.tmdb_id}" target="_blank" rel="noopener">TMDB</a>` : "";
      return `<div class="lrow"><div style="min-width:0">${title}${e.detail || tmdb ? `<div class="small muted">${esc(e.detail)}${tmdb}</div>` : ""}</div>
        <div class="meta"><span class="badge ${cls}">${label}</span><div>${esc(e.source_label)} · ${when(e.ts)}</div></div></div>`;
    }).join("") : `<div class="empty">Nothing logged${kind || watchlist ? " for this filter" : " yet"}</div>`;
    $("#runs").innerHTML = runs.length ? runs.map((r) => {
      const seg = segments(r);
      const trigger = (r.trigger === "schedule" ? "Scheduled" : "Manual") + (r.dry_run ? " · dry run" : "");
      const stateCls = r.status === "failed" ? "err" : "";
      return `<div class="lrow run"><div class="grow"><div><span style="font-weight:500">${esc(r.label)}</span> <span class="small dim">· ${trigger}</span></div>
        ${barHtml(seg)}${r.message ? `<div class="small ${stateCls}" style="margin-top:5px;${stateCls ? "" : "color:var(--muted)"}">${esc(r.message)}</div>` : ""}</div>
        <div class="meta">${when(r.started_at)}<div>${r.scanned} checked</div></div></div>`;
    }).join("") : `<div class="empty">No syncs yet</div>`;
  } catch (e) { toast(e.message); }
}

/* ---------- settings ---------- */

async function renderSettings() {
  $("#page").innerHTML = `<div class="page-head"><h1>Settings</h1></div>
    <form id="settings-form" autocomplete="off">
      <div class="card">
        <h2>Sync</h2>
        <label class="check"><input type="checkbox" name="schedule_enabled"><span>Sync automatically every day</span></label>
        <label class="field" for="schedule_time">Time</label>
        <div class="row" style="flex-wrap:nowrap"><input id="schedule_time" name="schedule_time" type="time" class="narrow"><span class="small dim">${esc(status.timezone)}, from the TZ variable</span></div>
        <label class="check"><input type="checkbox" name="dry_run"><span>Dry run<div class="hint">Syncs only log what they would request and send nothing to Seerr. Use it to check a first sync.</div></span></label>
      </div>
      <div class="card">
        <h2>Security</h2>
        <p class="small muted" id="pw-state" style="margin-top:4px"></p>
        <label class="field" for="password">New password</label>
        <input id="password" name="password" type="password" autocomplete="new-password" placeholder="At least 8 characters">
        <div class="card-actions" id="pw-actions" style="justify-content:flex-start"></div>
      </div>
      <details class="card">
        <summary style="cursor:pointer"><h2 style="display:inline">Advanced</h2></summary>
        <label class="field" for="letterboxd_delay">Seconds between Letterboxd requests</label>
        <input id="letterboxd_delay" name="letterboxd_delay" type="number" step="0.5" min="0.5" class="narrow">
        <label class="field" for="log_retention_days">Keep logs for</label>
        <div class="row" style="flex-wrap:nowrap"><input id="log_retention_days" name="log_retention_days" type="number" min="1" class="narrow"><span class="small dim">days</span></div>
      </details>
      <div class="row" style="justify-content:flex-end"><button type="submit" class="primary">Save</button></div>
    </form>`;
  const form = $("#settings-form");
  try { settingsCache = await api("/api/settings"); } catch (e) { toast(e.message); return; }
  const fill = () => {
    form.schedule_enabled.checked = settingsCache.schedule_enabled;
    form.schedule_time.value = settingsCache.schedule_time;
    form.dry_run.checked = settingsCache.dry_run;
    form.letterboxd_delay.value = settingsCache.letterboxd_delay;
    form.log_retention_days.value = settingsCache.log_retention_days;
    form.password.value = "";
    $("#pw-state").textContent = settingsCache.password_enabled
      ? "A password is set. Enter a new one to change it."
      : "No password set. Anyone who can open this page can use your Seerr connection.";
    $("#pw-actions").innerHTML = settingsCache.password_enabled ? `<button type="button" class="danger" id="clear-pw">Remove password</button>` : "";
    $("#clear-pw")?.addEventListener("click", clearPassword);
  };
  async function clearPassword() {
    if (!confirm("Remove the password? Anyone who can open this page will be able to use it.")) return;
    try {
      settingsCache = await api("/api/settings", { method: "PUT", json: { clear_password: true } });
      fill();
      toast("Password removed");
      refresh();
    } catch (e) { toast(e.message); }
  }
  fill();
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const payload = {
      schedule_enabled: form.schedule_enabled.checked,
      schedule_time: form.schedule_time.value,
      dry_run: form.dry_run.checked,
      letterboxd_delay: Number(form.letterboxd_delay.value),
      log_retention_days: Number(form.log_retention_days.value),
    };
    if (form.password.value) payload.password = form.password.value;
    try {
      settingsCache = await api("/api/settings", { method: "PUT", json: payload });
      fill();
      toast(payload.password ? "Saved. The password is now required" : "Saved");
      refresh();
    } catch (err) { toast(err.message); }
  });
}

/* ---------- render loop ---------- */

function renderPage() {
  if (!status) return;
  renderWatchlists.last = null;
  renderShell();
  if (route.page === "watchlists") renderWatchlists();
  else if (route.page === "connections") renderConnections();
  else if (route.page === "logs") renderLogs();
  else renderSettings();
}

function render() {
  renderShell();
  if (route.page === "watchlists") {
    if (!(editing && dragged)) renderWatchlists();
  } else if (route.page === "connections") updateConnectionsStatus();
  else if (route.page === "logs" && (status.running || wasRunning) && Date.now() - logsLoadedAt > 4000) loadLogs();
}

async function refresh() {
  clearTimeout(refresh.timer);
  try {
    const first = !status;
    status = await api("/api/status");
    if (first) renderPage(); else render();
    const checking = (status.seerr.configured && !status.seerr.check) || status.watchlists.some((w) => !w.check);
    refresh.timer = setTimeout(refresh, status.running || checking ? 1500 : 15000);
    wasRunning = status.running;
  } catch (e) {
    $("#foot-state").innerHTML = `<span class="dot red"></span><span>Server unreachable</span>`;
    refresh.timer = setTimeout(refresh, 5000);
  }
}

parseRoute();
refresh();
