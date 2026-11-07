/* seedmo – web-UI */
(() => {
"use strict";
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const STAGES = ["Metadata", "Henter", "Seeder", "Færdig"];
const FILTERS = {
  all: { title: "Alle", test: () => true },
  active: { title: "Henter", test: t => t.stage <= 2 && t.stage > 0 && !t.paused && t.status !== "Vælg filer" },
  choose: { title: "Vælg filer", test: t => t.status === "Vælg filer" },
  seeding: { title: "Seeder", test: t => t.stage === 3 && !t.paused },
  done: { title: "Færdige", test: t => t.stage === 4 },
  paused: { title: "På pause", test: t => t.paused && t.stage < 4 && t.stage > 0 },
  error: { title: "Fejl", test: t => t.kind === "problem" },
};

let state = { torrents: [], filter: "all", selected: null, detailFiles: null };

// ---------- formatering ----------
const nf1 = new Intl.NumberFormat("da-DK", { maximumFractionDigits: 1, minimumFractionDigits: 0 });
function bytes(n) {
  if (!n) return "0 B";
  const u = ["B", "KB", "MB", "GB", "TB"]; let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return nf1.format(n) + " " + u[i];
}
const rate = n => bytes(n) + "/s";
function eta(s) {
  if (s == null) return "–";
  if (s < 60) return s + " SEK";
  if (s < 3600) return Math.round(s / 60) + " MIN";
  if (s < 86400) return Math.floor(s / 3600) + " T " + Math.round((s % 3600) / 60) + " M";
  return Math.round(s / 86400) + " DAGE";
}
const two = n => String(n).padStart(2, "0");
const pct = p => nf1.format(Math.floor(p * 1000) / 10) + " %";

function toast(msg, ms = 3200) {
  const el = document.createElement("div");
  el.className = "toast"; el.textContent = msg; document.body.appendChild(el);
  setTimeout(() => el.remove(), ms);
}

// ---------- API ----------
async function api(path, opts = {}) {
  const o = { credentials: "same-origin", ...opts };
  if (o.json !== undefined) { o.body = JSON.stringify(o.json); o.headers = { "Content-Type": "application/json" }; delete o.json; }
  const r = await fetch(path, o);
  if (r.status === 401 && path !== "/api/login") { showLogin(); throw new Error("Log ind først"); }
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.detail || data.error || "Noget gik galt");
  return data;
}

// ---------- tema ----------
function applyTheme() {
  const t = localStorage.getItem("seedmo.theme") || "signal";
  const m = localStorage.getItem("seedmo.mode") || "system";
  const dark = m === "dark" || (m === "system" && matchMedia("(prefers-color-scheme: dark)").matches);
  document.documentElement.dataset.theme = t + (dark ? "-dark" : "-light");
  document.documentElement.dataset.family = t;
  $('meta[name="theme-color"]').content = getComputedStyle(document.documentElement).getPropertyValue("--ground").trim();
}
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", applyTheme);

// ---------- login ----------
function showLogin() { $("#app").classList.add("hidden"); $("#login").classList.remove("hidden"); $("#pw").focus(); }
function showApp() { $("#login").classList.add("hidden"); $("#app").classList.remove("hidden"); }
$("#loginForm").addEventListener("submit", async e => {
  e.preventDefault(); $("#loginErr").textContent = "";
  try { await api("/api/login", { method: "POST", json: { password: $("#pw").value } }); $("#pw").value = ""; showApp(); refresh(); }
  catch (err) { $("#loginErr").textContent = err.message; }
});

// ---------- segmenter ----------
function segs(stage, big) {
  let h = `<div class="segs${big ? " big" : ""}" aria-hidden="true">`;
  for (let i = 1; i <= 4; i++) h += `<span class="${i <= stage ? "on" : ""}"></span>`;
  return h + "</div>";
}

// ---------- liste ----------
function rowMeta(t) {
  if (t.kind === "problem") return "";
  if (t.status === "Vælg filer") return `${bytes(t.total_size)} · KLAR`;
  if (t.stage === 1) return `${two(t.peers)} PEERS`;
  if (t.stage === 2) return `${pct(t.progress)} · ↓ ${rate(t.down_rate)}${t.eta != null ? " · " + eta(t.eta) : ""}`;
  return `${bytes(t.size)} · RATIO ${nf1.format(t.ratio)}${t.up_rate ? " · ↑ " + rate(t.up_rate) : ""}`;
}

function renderList() {
  const list = state.torrents.filter(FILTERS[state.filter].test);
  const rows = $("#rows");
  $("#listTitle").textContent = FILTERS[state.filter].title;
  $("#countLabel").textContent = list.length ? `${two(list.length)} ${list.length === 1 ? "TORRENT" : "TORRENTS"}` : "";
  if (!list.length) {
    rows.innerHTML = state.torrents.length
      ? `<div class="empty">Ingen torrents her.</div>`
      : `<div class="empty">Ingen torrents endnu. Indsæt et magnet-link ovenfor, eller træk en .torrent-fil ind.</div>`;
    return;
  }
  $$(".empty", rows).forEach(e => e.remove());
  const existing = new Map($$(".row-item", rows).map(el => [el.dataset.id, el]));
  const ids = list.map(t => t.id);
  for (const [id, el] of existing) if (!ids.includes(id)) el.remove();
  list.forEach((t, i) => {
    let el = existing.get(t.id);
    if (!el) {
      el = document.createElement("button");
      el.className = "row-item"; el.dataset.id = t.id; el.type = "button";
      el.addEventListener("click", () => select(t.id));
    }
    const html = `<div class="t">${esc(t.name)}</div>
      <div class="s"><span class="status-word k-${t.kind}">${esc(t.status)}</span><span class="meta">${rowMeta(t)}</span></div>
      ${segs(t.stage)}`;
    if (el._html !== html) { el.innerHTML = html; el._html = html; }
    el.setAttribute("aria-current", String(t.id === state.selected));
    el.setAttribute("aria-label", `${t.name}, ${t.status}`);
    if (rows.children[i] !== el) rows.insertBefore(el, rows.children[i] || null);
  });
}

function renderCounts() {
  for (const [k, f] of Object.entries(FILTERS)) {
    const n = state.torrents.filter(f.test).length;
    const el = $(`[data-n="${k}"]`); if (el) el.textContent = two(n);
  }
  $('#nav [data-f="error"]').classList.toggle("hidden", !state.torrents.some(FILTERS.error.test));
  $('#nav [data-f="choose"]').classList.toggle("hidden", !state.torrents.some(FILTERS.choose.test));
}

$("#nav").addEventListener("click", e => {
  const b = e.target.closest("button[data-f]"); if (!b) return;
  state.filter = b.dataset.f;
  $$("#nav button").forEach(x => x.setAttribute("aria-current", String(x === b)));
  renderList();
});

// ---------- detalje ----------
async function select(id) {
  state.selected = id; state.detailFiles = null;
  history.replaceState(null, "", "#" + id);
  renderList();
  $("#detail").classList.remove("mobile-hidden");
  await loadDetail(true);
}

function closeDetail() {
  state.selected = null; state.detailFiles = null;
  history.replaceState(null, "", location.pathname);
  $("#detail").classList.add("mobile-hidden");
  $("#detail").innerHTML = `<div class="placeholder">Vælg en torrent</div>`;
  renderList();
}

async function loadDetail(full) {
  if (!state.selected) return;
  let d;
  try { d = await api("/api/torrents/" + state.selected); }
  catch (e) { closeDetail(); return; }
  const box = $("#detail");
  const fileKey = d.files.map(f => f.index).join(",");
  if (full || !box.querySelector(".dhead") || state.detailFiles !== fileKey) {
    state.detailFiles = fileKey;
    box.innerHTML = `
      <button class="btn back" type="button" id="back">← Tilbage</button>
      <div class="dhead"></div>
      <div class="actions" id="actions"></div>
      <div class="files" id="files"></div>
      <div id="magnetBox"></div>`;
    $("#back").addEventListener("click", closeDetail);
    renderFiles(d);
  }
  renderDetailHead(d);
  updateFiles(d);
}

function renderDetailHead(d) {
  const labels = STAGES.map((s, i) => `<span class="label" style="${i + 1 === d.stage ? "color:var(--ink)" : ""}">${s}</span>`).join("");
  const stats = [
    ["Hentet", bytes(d.done)],
    ["Størrelse", bytes(d.status === "Vælg filer" ? d.total_size : d.size)],
    ["Fremdrift", d.status === "Vælg filer" ? "–" : pct(d.progress)],
    ["↓ Download", rate(d.down_rate)],
    ["↑ Upload", rate(d.up_rate)],
    ["Peers · Seeds", `${two(d.peers)} · ${two(d.seeds)}`],
    ["Ratio", nf1.format(d.ratio)],
    ["Tid tilbage", eta(d.eta)],
  ];
  const head = `
    <h2 class="k-${d.kind}">${esc(d.status)}</h2>
    <div class="name">${esc(d.name)}</div>
    ${segs(d.stage, true)}
    <div class="seg-labels">${labels}</div>
    <div class="stats">${stats.map(([k, v]) => `<div><span class="label">${k}</span><b>${v}</b></div>`).join("")}</div>`;
  const h = $("#detail .dhead");
  if (h._html !== head) { h.innerHTML = head; h._html = head; }

  const canStart = d.paused || d.stage === 4;
  const act = `
    ${canStart ? `<button class="btn primary" data-a="resume" type="button">${d.stage === 4 ? "Seed igen" : "Start"}</button>`
               : `<button class="btn" data-a="pause" type="button">Pause</button>`}
    <button class="btn danger" data-a="remove" type="button">Fjern</button>`;
  const a = $("#actions");
  if (a._html !== act) { a.innerHTML = act; a._html = act; }

  const mb = $("#magnetBox");
  const mag = d.magnet ? `<div class="magnet"><span class="label">Gemt i</span><code>${esc(d.save_path)}</code></div>` : "";
  if (mb._html !== mag) { mb.innerHTML = mag; mb._html = mag; }
}

function renderFiles(d) {
  const box = $("#files");
  if (!d.has_metadata) { box.innerHTML = `<div class="empty" style="padding:20px 0">Filerne vises, når metadata er hentet.</div>`; return; }
  const many = d.files.length > 1;
  box.innerHTML = `
    <div class="files-head"><span class="label">${two(d.files.length)} ${d.files.length === 1 ? "FIL" : "FILER"}</span>
      ${many ? `<span class="btns"><button class="linkbtn" data-sel="all" type="button">Vælg alle</button><button class="linkbtn" data-sel="none" type="button">Fravælg alle</button></span>` : ""}</div>
    ${d.files.map(f => `
      <div class="file${f.wanted ? "" : " off"}" data-i="${f.index}">
        <label><input type="checkbox" ${f.wanted ? "checked" : ""} ${many ? "" : "disabled"} aria-label="Hent ${esc(f.path)}">
        <span class="p">${esc(f.path)}</span></label>
        <span class="sz" data-sz></span>
      </div>`).join("")}`;
  box.addEventListener("change", onFileToggle);
  box.addEventListener("click", onSelAll);
}

function updateFiles(d) {
  for (const f of d.files) {
    const row = $(`.file[data-i="${f.index}"]`); if (!row) continue;
    const p = f.size ? Math.floor((f.done / f.size) * 100) : 100;
    const txt = f.wanted ? (p >= 100 ? bytes(f.size) : `${p} % · ${bytes(f.size)}`) : bytes(f.size);
    const sz = $("[data-sz]", row); if (sz.textContent !== txt) sz.textContent = txt;
  }
}

let fileTimer = null;
function queueFileSave() {
  clearTimeout(fileTimer);
  fileTimer = setTimeout(async () => {
    const wanted = {};
    $$("#files .file").forEach(r => wanted[r.dataset.i] = $("input", r).checked);
    if (!Object.values(wanted).some(Boolean)) { toast("Vælg mindst én fil"); return; }
    try { await api(`/api/torrents/${state.selected}/files`, { method: "POST", json: { wanted } }); }
    catch (e) { toast(e.message); }
  }, 400);
}
function onFileToggle(e) {
  const r = e.target.closest(".file"); if (!r) return;
  r.classList.toggle("off", !e.target.checked);
  queueFileSave();
}
function onSelAll(e) {
  const b = e.target.closest("[data-sel]"); if (!b) return;
  const on = b.dataset.sel === "all";
  $$("#files .file").forEach(r => { $("input", r).checked = on; r.classList.toggle("off", !on); });
  if (on) queueFileSave(); else toast("Vælg de filer, du vil hente");
}

$("#detail").addEventListener("click", async e => {
  const b = e.target.closest("[data-a]"); if (!b) return;
  const id = state.selected;
  try {
    if (b.dataset.a === "pause") await api(`/api/torrents/${id}/pause`, { method: "POST" });
    if (b.dataset.a === "resume") await api(`/api/torrents/${id}/resume`, { method: "POST" });
    if (b.dataset.a === "remove") {
      const t = state.torrents.find(x => x.id === id);
      $("#removeName").textContent = t ? t.name : "";
      $("#removeFiles").checked = false;
      const dlg = $("#confirmRemove"); dlg.returnValue = ""; dlg.showModal();
      dlg.addEventListener("close", async () => {
        if (dlg.returnValue !== "ok") return;
        try { await api(`/api/torrents/${id}?delete_files=${$("#removeFiles").checked}`, { method: "DELETE" }); closeDetail(); refresh(); }
        catch (err) { toast(err.message); }
      }, { once: true });
      return;
    }
    refresh();
  } catch (err) { toast(err.message); }
});

// ---------- tilføj ----------
async function addLink(link) {
  try { const r = await api("/api/add", { method: "POST", json: { link } }); await refresh(); select(r.id); return true; }
  catch (e) { toast(e.message); return false; }
}
$("#addForm").addEventListener("submit", async e => {
  e.preventDefault();
  const v = $("#addLink").value.trim(); if (!v) return;
  if (await addLink(v)) $("#addLink").value = "";
});
$("#pickFile").addEventListener("click", () => $("#fileInput").click());
$("#fileInput").addEventListener("change", e => { uploadFiles(e.target.files); e.target.value = ""; });

async function uploadFiles(files) {
  const list = [...files].filter(f => /\.torrent$/i.test(f.name) || f.type === "application/x-bittorrent");
  if (!list.length) { toast("Træk en .torrent-fil ind"); return; }
  const fd = new FormData(); list.forEach(f => fd.append("files", f));
  try {
    const r = await api("/api/upload", { method: "POST", body: fd });
    if (r.errors.length) toast(r.errors.join(" · "), 5000);
    await refresh(); if (r.ids[0]) select(r.ids[0]);
  } catch (e) { toast(e.message); }
}

// Træk og slip – .torrent-filer eller et magnet-link
let dropEl = null, dragDepth = 0;
addEventListener("dragenter", e => {
  if ($("#app").classList.contains("hidden")) return;
  e.preventDefault(); dragDepth++;
  if (!dropEl) { dropEl = document.createElement("div"); dropEl.className = "drop"; dropEl.textContent = "Slip for at tilføje"; document.body.appendChild(dropEl); }
});
addEventListener("dragover", e => e.preventDefault());
addEventListener("dragleave", () => { if (--dragDepth <= 0) { dragDepth = 0; dropEl?.remove(); dropEl = null; } });
addEventListener("drop", e => {
  e.preventDefault(); dragDepth = 0; dropEl?.remove(); dropEl = null;
  if (e.dataTransfer.files.length) uploadFiles(e.dataTransfer.files);
  else { const t = e.dataTransfer.getData("text/plain"); if (t && t.startsWith("magnet:")) addLink(t.trim()); }
});
// Indsæt et magnet-link hvor som helst på siden
addEventListener("paste", e => {
  if (e.target.closest("input,textarea") || $("#app").classList.contains("hidden")) return;
  const t = (e.clipboardData.getData("text") || "").trim();
  if (t.startsWith("magnet:")) { e.preventDefault(); addLink(t); }
});

// ---------- indstillinger ----------
$("#openSettings").addEventListener("click", async () => {
  try {
    const s = await api("/api/settings");
    const f = $("#settingsForm");
    for (const [k, v] of Object.entries(s)) {
      const el = f.elements[k]; if (!el) continue;
      if (el.type === "checkbox") el.checked = !!v; else el.value = v;
    }
    $("#themeSel").value = localStorage.getItem("seedmo.theme") || "signal";
    $("#modeSel").value = localStorage.getItem("seedmo.mode") || "system";
    $("#settings").showModal();
  } catch (e) { toast(e.message); }
});
$("#themeSel").addEventListener("change", e => { localStorage.setItem("seedmo.theme", e.target.value); applyTheme(); });
$("#modeSel").addEventListener("change", e => { localStorage.setItem("seedmo.mode", e.target.value); applyTheme(); });
$("#settings").addEventListener("close", async () => {
  if ($("#settings").returnValue !== "save") return;
  const f = $("#settingsForm");
  const body = {
    download_limit_kb: +f.elements.download_limit_kb.value || 0,
    upload_limit_kb: +f.elements.upload_limit_kb.value || 0,
    max_active_downloads: +f.elements.max_active_downloads.value || 3,
    seed_ratio_limit: f.elements.seed_ratio_limit.value === "" ? 1 : +f.elements.seed_ratio_limit.value,
    seed_time_limit_min: +f.elements.seed_time_limit_min.value || 0,
    start_paused: f.elements.start_paused.checked,
    limit_lan: f.elements.limit_lan.checked,
  };
  try { await api("/api/settings", { method: "POST", json: body }); toast("Gemt"); refresh(); }
  catch (e) { toast(e.message); }
});
$("#logout").addEventListener("click", async () => { await api("/api/logout", { method: "POST" }).catch(() => {}); $("#settings").close(); showLogin(); });
$("#regHandler").addEventListener("click", () => {
  if (!("registerProtocolHandler" in navigator)) { toast("Din browser understøtter det ikke (fx Safari). Brug Chrome eller Firefox.", 5000); return; }
  if (!isSecureContext) { toast("Kræver at seedmo åbnes via https. Se vejledningen.", 5000); return; }
  try { navigator.registerProtocolHandler("magnet", location.origin + "/add?magnet=%s"); toast("Godkend i browserens adresselinje"); }
  catch (e) { toast("Browseren afviste: " + e.message, 5000); }
});

// ---------- opdatering ----------
let busy = false;
async function refresh() {
  if (busy) return; busy = true;
  try {
    const d = await api("/api/torrents");
    state.torrents = d.torrents;
    renderCounts(); renderList();
    const down = d.torrents.reduce((a, t) => a + t.down_rate, 0), up = d.torrents.reduce((a, t) => a + t.up_rate, 0);
    $("#sumDown").textContent = rate(down) + (d.limits.down ? ` / ${d.limits.down} KB` : "");
    $("#sumUp").textContent = rate(up) + (d.limits.up ? ` / ${d.limits.up} KB` : "");
    $("#diskFree").textContent = bytes(d.disk.free);
    $("#diskBar").style.width = (d.disk.used / d.disk.total * 100).toFixed(1) + "%";
    if (state.selected) {
      if (!d.torrents.some(t => t.id === state.selected)) closeDetail(); else await loadDetail(false);
    }
  } catch (e) { /* login vises af api() */ }
  finally { busy = false; }
}

async function boot() {
  applyTheme();
  const q = new URLSearchParams(location.search);
  if (q.get("fejl")) { toast(q.get("fejl"), 5000); history.replaceState(null, "", "/"); }
  const a = await fetch("/api/auth").then(r => r.json()).catch(() => ({ ok: false, required: true }));
  if (!a.ok) { showLogin(); return; }
  showApp();
  await refresh();
  const h = location.hash.slice(1);
  if (h && state.torrents.some(t => t.id === h)) select(h);
}
setInterval(() => { if (!document.hidden && !$("#app").classList.contains("hidden")) refresh(); }, 2000);
document.addEventListener("keydown", e => { if (e.key === "Escape" && state.selected && innerWidth <= 860) closeDetail(); });
boot();
})();
