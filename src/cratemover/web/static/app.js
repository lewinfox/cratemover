"use strict";

const $ = (id) => document.getElementById(id);
const state = {
  tabs: [], active: null,         // one tab per library or drive; "other" is the open-a-folder tab
  sources: [], extra: [],         // libraries the server found, and ones opened by hand
  drives: [], roots: [], backups: [],
  libs: {},                       // tab key -> loaded library (the /api/inspect result)
  selected: new Set(), currentPlaylist: "",
  wizard: null,                   // the open action: { kind: export|import|convert, src, dst, ... }
};

const NAMES = {
  mixxx: "Mixxx", rekordbox_db: "Rekordbox", rekordbox_usb: "Rekordbox",
  rekordbox_xml: "Rekordbox XML", serato: "Serato",
};
const STICK_FORMATS = { rekordbox_usb: "Rekordbox", serato: "Serato" };

const NEXT_STEPS = {
  rekordbox_xml: `<h4>Importing into Rekordbox</h4><ol>
    <li>Rekordbox › Preferences › Advanced › Database › <i>rekordbox xml</i> › Imported Library: choose the file above.</li>
    <li>It appears under <b>rekordbox xml</b> in the tree (enable it under Preferences › View › Layout if not).</li>
    <li>Right-click playlists there › <i>Import Playlist</i>.</li></ol>`,
  rekordbox_usb: `<h4>Using the stick</h4><ol>
    <li>Eject it safely, then plug it into a player, or into the laptop (Rekordbox shows it under Devices; Mixxx lists it under Rekordbox). Pre-NXS2 players only play MP3 reliably.</li>
    <li>If something looks wrong, the previous <code>export.pdb</code> is kept next to the new one with a <code>.cratemover-…</code> suffix.</li></ol>`,
  serato: `<h4>Using it in Serato</h4><ol>
    <li>Start Serato. Cue points, loops and beat grids come from the files' tags, if you chose to write them.</li></ol>`,
  mixxx: `<h4>Using it in Mixxx</h4><ol>
    <li>Start Mixxx; a backup of the old database sits next to it.</li></ol>`,
};

async function api(path, options = {}) {
  const response = await fetch(path, options);
  if (!response.ok) {
    let detail = response.statusText;
    try { detail = (await response.json()).detail || detail; } catch (_) { /* not JSON */ }
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return response.json();
}

const post = (path, body) => api(path, {
  method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
});

// A collapsible log under a status line, filled from the job's log as it runs.
function jobLog(statusEl) {
  // Loading a library reports through a stand-in that only shows on the active tab: no log there.
  if (!(statusEl instanceof Element)) return Object.assign(document.createElement("details"), { innerHTML: "<pre></pre>" });
  let el = statusEl.nextElementSibling;
  if (!el || !el.classList.contains("job-log")) {
    el = document.createElement("details");
    el.className = "job-log";
    el.innerHTML = "<summary>Log</summary><pre></pre>";
    statusEl.after(el);
  }
  return el;
}

async function waitForJob(jobId, statusEl) {
  const log = jobLog(statusEl);
  log.open = false;
  for (;;) {
    const job = await api(`/api/jobs/${jobId}`);
    const pre = log.querySelector("pre");
    const atEnd = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 4;
    pre.textContent = job.log.join("\n");
    if (atEnd) pre.scrollTop = pre.scrollHeight;
    if (job.status === "done") return job.result;
    if (job.status === "error") {
      log.open = true;
      throw new Error(job.error);
    }
    statusEl.textContent = job.message || "Working…";
    await new Promise((r) => setTimeout(r, 500));
  }
}

function setStatus(el, text, error = false) {
  el.textContent = text;
  el.classList.toggle("error", error);
}

function escapeHtml(text) {
  return String(text ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function stat(label, value) {
  return `<div class="stat"><b>${escapeHtml(value)}</b><span>${escapeHtml(label)}</span></div>`;
}

function gb(bytes) {
  return bytes >= 1e9 ? `${(bytes / 1e9).toFixed(1)} GB` : `${Math.round(bytes / 1e6)} MB`;
}

const warningList = (warnings) => `<ul class="warnings">${warnings.map((w) => `<li>${escapeHtml(w)}</li>`).join("")}</ul>`;

// --- tabs: one per library, one per drive without a library -----------------------------------

function buildTabs() {
  const tabs = [];
  for (const s of [...state.sources, ...state.extra]) {
    const key = `${s.format}:${s.path}`;
    if (tabs.some((t) => t.key === key)) continue;
    // Windows drives are E:\ and their folders E:\_Serato_, so compare with one kind of slash.
    const slash = (p) => p.replaceAll("\\", "/").replace(/\/$/, "");
    const drive = state.drives.find((d) => slash(s.path) === slash(d.path) || slash(s.path).startsWith(slash(d.path) + "/"));
    let label = NAMES[s.format] || "Library";
    if (drive) label = `USB: ${drive.label} (${label})`;
    else if (s.format === "rekordbox_xml") label = `XML: ${s.path.split(/[\\/]/).pop()}`;
    tabs.push({ key, ...s, drive, label });
  }
  for (const d of state.drives) {
    if (!tabs.some((t) => t.drive === d)) {
      tabs.push({ key: `drive:${d.path}`, format: "", path: d.path, drive: d, label: `USB: ${d.label} (no library)` });
    }
  }
  for (const t of tabs) {
    if (tabs.filter((o) => o.label === t.label).length > 1) t.label += ` — ${t.path}`;
  }
  return tabs.sort((a, b) => Boolean(a.drive) - Boolean(b.drive));
}

const ICON = (paths) => `<svg class="icon" viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">${paths}</svg>`;
const ICONS = {
  database: ICON('<ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v14c0 1.7 3.6 3 8 3s8-1.3 8-3V5"/><path d="M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3"/>'),
  usb: ICON('<rect x="6" y="9" width="12" height="13" rx="2"/><path d="M8 9V2h8v7"/><path d="M11 5v1M13 5v1"/>'),
};

const tabByKey = (key) => state.tabs.find((t) => t.key === key);
const activeTab = () => tabByKey(state.active);

function renderTabs() {
  $("tabs").innerHTML = state.tabs.map((t) =>
    `<button type="button" role="tab" class="${t.drive ? "usb" : "local"}" data-key="${escapeHtml(t.key)}" aria-selected="${t.key === state.active}">${t.drive ? ICONS.usb : ICONS.database}${escapeHtml(t.label.replace(/^USB: /, ""))}</button>`).join("") +
    `<button type="button" role="tab" data-key="other" aria-selected="${state.active === "other"}">Other…</button>`;
}

function selectTab(key) {
  state.active = key;
  state.wizard = null;
  state.selected = new Set();
  renderTabs();
  renderHead();
  renderWizard();
  renderBrowser();
  const tab = activeTab();
  if (tab && tab.format && !state.libs[tab.key]) loadTab(tab).catch(() => {});
}

async function refresh() {
  const [drives, sources] = await Promise.all([api("/api/drives"), api("/api/sources")]);
  state.drives = drives.drives;
  state.roots = drives.roots;
  state.sources = sources.sources;
  try { state.backups = (await api("/api/backups")).backups; } catch (_) { state.backups = []; }
  state.tabs = buildTabs();
  if (state.active !== "other" && !activeTab()) {
    selectTab(state.tabs.length ? state.tabs[0].key : "other");
  } else {
    renderTabs();
    renderHead();
    renderWizard();
  }
}

function watchDrives() {
  const sign = (drives) => drives.map((d) => `${d.path}:${d.libraries.map((l) => l.format).join(",")}:${d.writable}`).join("\n");
  const tick = async () => {
    try {
      const { drives } = await api("/api/drives");
      if (sign(drives) !== sign(state.drives)) await refresh();
    } catch (_) { /* try again next time */ }
    setTimeout(tick, 3000);
  };
  setTimeout(tick, 3000);
}

// --- loading and browsing a library ------------------------------------------------------------

const readRequest = (tab) => ({
  format: tab.format, path: tab.path, serato_root: tab.serato_root || "/", mp3_decoder: $("set-mp3").value,
});

const loading = {};

function loadTab(tab) {
  if (loading[tab.key]) return loading[tab.key];
  const status = { set textContent(t) { if (state.active === tab.key) $("view-status").textContent = t; } };
  const show = (text, error) => { if (state.active === tab.key) setStatus($("view-status"), text, error); };
  show("Loading…");
  loading[tab.key] = (async () => {
    try {
      const { job_id } = await post("/api/inspect", readRequest(tab));
      const lib = await waitForJob(job_id, status);
      state.libs[tab.key] = lib;
      const found = lib.access_rules.map(([from, to]) => `${from} → ${to}`).join(", ");
      show(found ? `Music files found: ${found}.` : "");
      if (state.active === tab.key) renderBrowser();
      return lib;
    } catch (err) {
      show(err.message, true);
      throw err;
    } finally {
      delete loading[tab.key];
    }
  })();
  return loading[tab.key];
}

const ensureLoaded = (tab) => (state.libs[tab.key] ? Promise.resolve(state.libs[tab.key]) : loadTab(tab));

function backupsFor(drive) {
  return state.backups.filter((b) => b.drive === drive.path || b.label === drive.label);
}

function renderHead() {
  const tab = activeTab();
  $("view").classList.toggle("hidden", !tab);
  $("other-card").classList.toggle("hidden", state.active !== "other");
  if (!tab) return;
  const d = tab.drive;
  $("view-title").textContent = tab.label;
  $("view-sub").textContent = [tab.path, d ? `${d.fstype || "?"} · ${gb(d.free_bytes)} free of ${gb(d.total_bytes)}` : ""]
    .filter(Boolean).join(" · ");
  const buttons = [];
  if (tab.format) buttons.push('<button type="button" data-act="export">Export to…</button>');
  if (tab.format !== "rekordbox_xml") buttons.push('<button type="button" data-act="import">Import from…</button>');
  if (d && tab.format in STICK_FORMATS) {
    const other = Object.keys(STICK_FORMATS).find((f) => f !== tab.format);
    buttons.push(`<button type="button" data-act="convert">Convert to ${STICK_FORMATS[other]}…</button>`);
  }
  if (tab.format) buttons.push('<button type="button" class="secondary" data-act="reload">Reload</button>');
  $("view-actions").innerHTML = buttons.join(" ");
  $("view-notes").innerHTML = (d ? d.notes : []).map((n) => `<div class="note">${escapeHtml(n)}</div>`).join("") +
    (tab.format ? "" : '<p class="hint">No DJ library on this drive yet. Use “Import from…” to put one on it.</p>');
  const backups = d ? backupsFor(d) : [];
  $("view-backups").innerHTML = backups.length ? `<details><summary>${backups.length} backup(s) of this drive</summary><ul class="backups">${
    backups.map((b, k) => `<li>${escapeHtml(b.created)} · ${b.full ? "whole drive" : "library folders"} · ${gb(b.size_bytes)}
      <button type="button" class="chip" data-restore="${k}">Restore</button></li>`).join("")}</ul></details>` : "";
}

function renderBrowser() {
  const tab = activeTab();
  const lib = tab && state.libs[tab.key];
  $("browser-pane").classList.toggle("hidden", !lib);
  if (!lib) return;
  const s = lib.summary;
  $("stats").innerHTML = stat("tracks", s.tracks) + stat("playlists & crates", s.playlists) +
    stat("hot cues", s.hot_cues) + stat("memory cues & loops", s.memory_cues) +
    stat("with beat grid", s.gridded) + stat("files not found", s.missing_files);
  const warnings = [...lib.warnings];
  if (s.missing_files) {
    warnings.push(`${s.missing_files} music file(s) aren't in any mounted folder, e.g. ${lib.missing_examples.slice(0, 2).join(", ")}.`);
  }
  $("lib-warnings").innerHTML = warnings.map((w) => `<li>${escapeHtml(w)}</li>`).join("");
  $("tree").innerHTML = lib.playlists.length ? renderTree(lib.playlists) : '<p class="hint">No playlists.</p>';
  showTracks("");
}

function renderTree(nodes) {
  return "<ul>" + nodes.map((n) => {
    if (n.children) {
      return `<li><label><input type="checkbox" data-folder="${escapeHtml(n.path)}"> <span class="folder">${escapeHtml(n.name)}</span></label>${renderTree(n.children)}</li>`;
    }
    return `<li><input type="checkbox" data-path="${escapeHtml(n.path)}"> <span class="name" data-path="${escapeHtml(n.path)}">${escapeHtml(n.name)}</span> <span class="count">${n.count}${n.crate ? " · crate" : ""}</span></li>`;
  }).join("") + "</ul>";
}

function collectSelection() {
  state.selected = new Set([...$("tree").querySelectorAll("input[data-path]:checked")].map((b) => b.dataset.path));
  renderWizard();
}

function selectAll(on) {
  $("tree").querySelectorAll("input[type=checkbox]").forEach((b) => { b.checked = on; });
  collectSelection();
}

async function showTracks(playlist) {
  const lib = state.libs[state.active];
  if (!lib) return;
  state.currentPlaylist = playlist;
  $("tracks-title").textContent = playlist || "All tracks";
  $("track-detail").classList.add("hidden");
  const q = $("track-search").value.trim();
  let data;
  try {
    data = await api(`/api/libraries/${lib.library_id}/tracks?playlist=${encodeURIComponent(playlist)}&q=${encodeURIComponent(q)}`);
  } catch (_) {
    // The server forgot the library (it restarted): read it again.
    delete state.libs[state.active];
    return void loadTab(activeTab()).catch(() => {});
  }
  $("tracks").innerHTML = data.tracks.map((t) =>
    `<tr data-id="${escapeHtml(t.id)}" title="${escapeHtml(t.location)}"><td>${escapeHtml(t.artist)}</td><td>${escapeHtml(t.title)}</td>
     <td>${t.bpm || ""}</td><td>${t.hot_cues || ""}</td><td>${t.memory_cues || ""}</td><td>${t.grid || ""}</td></tr>`).join("");
  $("tracks-more").textContent = data.total > data.tracks.length ? `Showing ${data.tracks.length} of ${data.total}.` : `${data.total} track(s).`;
}

async function showTrack(id) {
  const lib = state.libs[state.active];
  const t = await api(`/api/libraries/${lib.library_id}/tracks/${encodeURIComponent(id)}`);
  const lines = [`${t.artist} - ${t.title}`, t.location, `BPM ${t.bpm}  key ${t.key || "-"}`];
  if (t.grid.length) lines.push("Grid: " + t.grid.map((g) => `${(g.position_ms / 1000).toFixed(3)}s @ ${g.bpm.toFixed(2)} (beat ${g.beat})`).join(", "));
  for (const c of [...t.cues].sort((a, b) => a.position_ms - b.position_ms)) {
    const slot = c.slot === null ? "memory" : `hot ${String.fromCharCode(65 + c.slot)}`;
    const end = c.end_ms !== null ? `–${(c.end_ms / 1000).toFixed(3)}s` : "";
    const colour = c.colour !== null ? ` #${c.colour.toString(16).padStart(6, "0")}` : "";
    lines.push(`  ${c.role.padEnd(6)} ${slot.padEnd(7)} ${(c.position_ms / 1000).toFixed(3)}s${end} ${c.name || ""}${colour}`);
  }
  $("track-detail").textContent = lines.join("\n");
  $("track-detail").classList.remove("hidden");
}

// --- export to / import from --------------------------------------------------------------

// Where a library can be sent. `exists` means there is a library to merge into (with a preview);
// otherwise a new one is written.
function targetFor(tab, newFormat) {
  if (!tab.format) {
    return { id: `new:${newFormat}:${tab.path}`, format: newFormat, path: tab.path, exists: false,
      label: `USB: ${tab.drive.label}, as a new ${STICK_FORMATS[newFormat]} stick`, hint: tab.path };
  }
  if (tab.format === "rekordbox_db") {
    return { id: `xml:${tab.key}`, format: "rekordbox_xml", viaKey: tab.key, exists: false, label: "Rekordbox on this computer",
      hint: "Makes an XML file that you then import inside Rekordbox. Rekordbox doesn't let other programs write to its library." };
  }
  return { id: tab.key, format: tab.format, path: tab.path, tabKey: tab.key, exists: true, label: tab.label, hint: tab.path };
}

function targetsFor(tab) {
  const out = [];
  for (const t of state.tabs) {
    if (t.key !== tab.key && t.format && t.format !== "rekordbox_xml") out.push(targetFor(t));
  }
  for (const d of state.drives) {
    if (tab.drive === d) continue;
    for (const f of Object.keys(STICK_FORMATS)) {
      if (!d.libraries.some((l) => l.format === f)) out.push(targetFor({ format: "", path: d.path, drive: d }, f));
    }
  }
  out.push({ id: "xml", format: "rekordbox_xml", exists: false, label: "A Rekordbox XML file", hint: "Saved in the export folder, to import in Rekordbox on any computer." });
  return out;
}

function wizardEnds() {
  const w = state.wizard;
  const tab = activeTab();
  if (w.kind === "export") return { src: tab, dst: w.dst };
  return { src: tabByKey(w.src), dst: targetFor(tab, $("wiz-newformat").value) };
}

function openWizard(kind) {
  state.wizard = { kind, src: null, dst: null, previewed: false, busy: false, done: false };
  $("wiz-report").innerHTML = "";
  $("cv-report").innerHTML = "";
  setStatus($("wiz-status"), "");
  setStatus($("cv-status"), "");
  renderWizard();
  $(kind === "convert" ? "convert-panel" : "wizard").scrollIntoView({ behavior: "smooth", block: "nearest" });
}

function renderWizard() {
  const w = state.wizard;
  const tab = activeTab();
  $("wizard").classList.toggle("hidden", !w || !tab || w.kind === "convert");
  $("convert-panel").classList.toggle("hidden", !w || !tab || w.kind !== "convert");
  if (!w || !tab) return;
  if (w.kind === "convert") return renderConvertPanel();
  const exporting = w.kind === "export";
  $("wiz-title").textContent = exporting ? `Export from ${tab.label}` : `Import into ${tab.label}`;
  $("wiz-pick-label").textContent = exporting ? "1. Where to?" : "1. Where from?";
  w.choices = exporting ? targetsFor(tab)
    : state.tabs.filter((t) => t.format && t.key !== tab.key).map((t) => ({ id: t.key, label: t.label, hint: t.path }));
  const chosen = exporting ? (w.dst && w.dst.id) : w.src;
  $("wiz-choices").innerHTML = w.choices.map((c) =>
    `<button type="button" class="source" data-id="${escapeHtml(c.id)}" aria-pressed="${c.id === chosen}" ${w.busy || w.done ? "disabled" : ""}>
      <b>${escapeHtml(c.label)}</b><span>${escapeHtml(c.hint || "")}</span></button>`).join("")
    || '<p class="hint">No other library found. Plug in a stick, or open a library from the “Other…” tab.</p>';
  $("wiz-format").classList.toggle("hidden", exporting || Boolean(tab.format));

  const { src, dst } = wizardEnds();
  const ready = Boolean(src && dst);
  const merging = ready && dst.exists;
  const both = merging && $("wiz-both").checked;
  if (!exporting) {
    $("wiz-what").textContent = "The whole library. To bring over only some playlists, open that library's tab and use “Export to…”.";
  } else if (both) {
    $("wiz-what").textContent = "The whole library, both ways.";
  } else if (state.selected.size) {
    $("wiz-what").textContent = `${state.selected.size} ticked playlist(s) and the tracks in them.`;
  } else {
    $("wiz-what").textContent = "The whole library. Tick playlists in the list below to send only those.";
  }
  $("wiz-how").textContent = !ready ? ""
    : merging ? `Tracks, cues, grids and playlists are merged into ${dst.label}. Every file changed is backed up first.`
    : dst.format === "rekordbox_xml" ? (dst.hint || "")
    : `A new ${STICK_FORMATS[dst.format]} library is written onto the drive, and the music is copied onto it.`;
  // Only show options for the formats this run writes: the target, plus the source when syncing both ways.
  const writes = new Set(ready ? [dst.format, ...(both ? [src.format] : [])] : []);
  $("wiz-sync-opts").classList.toggle("hidden", !merging);
  $("wiz-serato-opts").classList.toggle("hidden", !writes.has("serato"));
  $("wiz-rekordbox-opts").classList.toggle("hidden", !writes.has("rekordbox_usb"));
  $("wiz-options").classList.toggle("hidden", !merging && !writes.has("serato") && !writes.has("rekordbox_usb"));
  $("wiz-preview").classList.toggle("hidden", !merging || w.done);
  $("wiz-preview").disabled = !ready || w.busy;
  $("wiz-run").classList.toggle("hidden", w.done);
  $("wiz-run").disabled = !ready || w.busy || (merging && !w.previewed);
  $("wiz-run").textContent = merging ? "4. Apply these changes"
    : dst && dst.format === "rekordbox_xml" ? "3. Make the XML file" : "3. Write to the stick";
  $("wiz-cancel").textContent = w.done ? "Close" : "Cancel";
}

function writeOptions() {
  return {
    mp3_decoder: $("set-mp3").value,
    serato_write_tags: $("wiz-serato-tags").checked,
    waveforms: $("wiz-waveforms").checked,
    onelibrary: $("wiz-onelibrary").value,
  };
}

function syncReport(result, srcLabel, dstLabel) {
  const parts = [];
  for (const [key, label] of [["a_to_b", `${srcLabel} → ${dstLabel}`], ["b_to_a", `${dstLabel} → ${srcLabel}`]]) {
    const side = result[key];
    if (!side) continue;
    const r = side.report;
    const by = Object.entries(r.matched_by).map(([k, v]) => `${v} by ${k}`).join(", ");
    const will = result.dry_run ? "to " : "";
    parts.push(`<h4>${escapeHtml(label)}</h4><div class="stats">${stat("tracks already there", r.matched)}${stat(`tracks ${will}add`, r.added)}` +
      `${stat(`tracks ${will}update`, r.updated)}${stat("new playlists", r.playlists_added)}` +
      `${stat("changed playlists", r.playlists_updated)}</div>` +
      (by ? `<p class="hint">Matched ${escapeHtml(by)}.</p>` : "") +
      (r.details.length ? `<details><summary>${r.details.length} change(s)</summary><ul>${r.details.map((d) => `<li>${escapeHtml(d)}</li>`).join("")}</ul></details>` : "") +
      (side.written ? warningList(side.written.warnings) : ""));
  }
  return parts.join("");
}

function convertReport(result) {
  const s = result.summary;
  const files = result.files.map((f) => f.download
    ? `<a href="/api/download?path=${encodeURIComponent(f.download)}">${escapeHtml(f.download)}</a>`
    : `<code>${escapeHtml(f.path)}</code>`).join(" ");
  return `<div class="stats">${stat("tracks", s.tracks)}${stat("playlists", s.playlists)}${stat("hot cues", s.hot_cues)}` +
    `${stat("memory cues & loops", s.memory_cues)}${stat("with beat grid", s.gridded)}</div>` +
    `<p>Written to <code>${escapeHtml(result.output_dir)}</code>:</p><div class="files">${files}</div>` +
    warningList(result.warnings) + (NEXT_STEPS[result.format] || "");
}

async function convertFrom(src, body, status) {
  for (let attempt = 0; ; attempt++) {
    const lib = await ensureLoaded(src);
    try {
      const { job_id } = await post("/api/convert", { ...body, library_id: lib.library_id });
      return await waitForJob(job_id, status);
    } catch (err) {
      // The server forgot the library (it restarted): read it again, once.
      if (attempt || !/load it again/.test(err.message)) throw err;
      delete state.libs[src.key];
    }
  }
}

async function transfer(dryRun) {
  const w = state.wizard;
  const { src, dst } = wizardEnds();
  const status = $("wiz-status");
  const playlists = w.kind === "export" ? [...state.selected] : [];
  if (!dryRun && dst.format !== "rekordbox_xml" &&
      !confirm(`This changes ${dst.label}. Is the DJ software that uses it closed?`)) return;
  w.busy = true;
  renderWizard();
  setStatus(status, dryRun ? "Comparing…" : "Working…");
  try {
    const changed = [];
    if (dst.exists) {
      const both = $("wiz-both").checked;
      const { job_id } = await post("/api/sync", {
        a: readRequest(src), b: readRequest(tabByKey(dst.tabKey)),
        direction: both ? "both" : "a_to_b",
        only_playlists: both ? [] : playlists,
        cues: $("wiz-cues").value, grids: $("wiz-grids").value,
        metadata: $("wiz-metadata").value, playlists: $("wiz-playlists").value,
        add_tracks: $("wiz-add").checked,
        dry_run: dryRun,
        write: { format: "mixxx", ...writeOptions() },
      });
      const result = await waitForJob(job_id, status);
      $("wiz-report").innerHTML = syncReport(result, src.label, dst.label);
      if (dryRun) w.previewed = true;
      else changed.push(dst.tabKey, ...(both ? [src.key] : []));
    } else {
      let rules = "";
      if (dst.viaKey) {
        // Write the paths as that Rekordbox sees them (e.g. C:/... under Wine).
        const via = await ensureLoaded(tabByKey(dst.viaKey));
        rules = via.access_rules.map(([from, to]) => `${to} => ${from}`).join("\n");
      }
      const result = await convertFrom(src, {
        ...writeOptions(), format: dst.format, playlists, path_rules: rules,
        in_place: dst.format !== "rekordbox_xml", target_path: dst.path || "",
        output_name: `rekordbox xml from ${src.label}`.replace(/[^\w -]/g, ""),
        serato_root: dst.format === "serato" ? dst.path : "/",
      }, status);
      $("wiz-report").innerHTML = convertReport(result);
    }
    if (dryRun) {
      setStatus(status, "Preview ready. Nothing has been changed yet.");
    } else {
      w.done = true;
      setStatus(status, "Done.");
      for (const key of changed) delete state.libs[key];
      await refresh();
    }
  } catch (err) {
    setStatus(status, err.message, true);
  } finally {
    w.busy = false;
    renderWizard();
  }
}

// --- convert a stick to the other format ---------------------------------------------------

function renderConvertPanel() {
  const w = state.wizard;
  const tab = activeTab();
  const d = tab.drive;
  const target = Object.keys(STICK_FORMATS).find((f) => f !== tab.format);
  const name = STICK_FORMATS[target];
  const source = STICK_FORMATS[tab.format];
  w.target = target;
  $("cv-title").textContent = `Convert ${d.label} from ${source} to ${name}`;
  $("cv-hint").textContent = `The ${source} library on this drive is replaced by a ${name} library that uses the music already on it. Nothing else is added to the drive.` +
    (target === "serato" ? " Serato keeps cues and grids inside the audio files, so those are updated." : "") +
    ` The library and all its music are copied to this computer first, byte for byte, so the drive can be put back exactly.`;
  $("cv-full-label").textContent = `Back up the whole drive (${gb(d.total_bytes - d.free_bytes)}), not just the library and its music`;
  $("cv-rekordbox").classList.toggle("hidden", target !== "rekordbox_usb");
  $("cv-run").classList.toggle("hidden", w.done);
  $("cv-run").disabled = w.busy;
  $("cv-cancel").textContent = w.done ? "Close" : "Cancel";
}

async function runDriveConvert() {
  const w = state.wizard;
  const tab = activeTab();
  const status = $("cv-status");
  w.busy = true;
  renderWizard();
  setStatus(status, "Starting…");
  try {
    const { job_id } = await post("/api/drives/convert", {
      path: tab.drive.path,
      target: w.target,
      source_format: tab.format,
      full_backup: $("cv-full").checked,
      onelibrary: $("cv-onelibrary").checked,
    });
    const r = await waitForJob(job_id, status);
    $("cv-report").innerHTML = `<p><b>Converted to ${escapeHtml(STICK_FORMATS[w.target])}.</b> Backup: <code>${escapeHtml(r.backup)}</code>` +
      (r.removed.length ? `<br>Removed the old ${escapeHtml(r.removed.join(", "))} library.` : "") + "</p>" + warningList([...(r.notes || []), ...r.warnings]);
    w.done = true;
    setStatus(status, "Done.");
    await refresh();
  } catch (err) {
    setStatus(status, err.message, true);
  } finally {
    w.busy = false;
    renderWizard();
  }
}

async function restoreBackup(index) {
  const drive = activeTab().drive;
  const backup = backupsFor(drive)[index];
  if (!confirm(`Restore ${drive.label} to how it was at ${backup.created}? Library changes since then are undone.`)) return;
  const status = $("view-status");
  try {
    const { job_id } = await post("/api/backups/restore", { backup: backup.path, path: drive.path });
    const result = await waitForJob(job_id, status);
    setStatus(status, `Restored ${drive.label}: ${result.done.join(", ")}.`);
    for (const t of state.tabs) if (t.drive === drive) delete state.libs[t.key];
    await refresh();
  } catch (err) {
    setStatus(status, `Restore failed: ${err.message}`, true);
  }
}

// --- opening a library that wasn't found automatically -------------------------------------

async function openBrowser(input) {
  const dialog = $("browser");
  let current = "";
  async function show(path) {
    let listing;
    try {
      listing = await api(`/api/browse?path=${encodeURIComponent(path)}`);
    } catch (_) {
      listing = await api("/api/browse?path=");
    }
    current = listing.path;
    $("browser-path").textContent = listing.path || "Available folders";
    const items = [];
    if (listing.parent !== null) items.push(`<li class="dir" data-path="${escapeHtml(listing.parent)}">..</li>`);
    for (const entry of listing.entries) {
      items.push(`<li class="${entry.dir ? "dir" : "file"}" data-path="${escapeHtml(entry.path)}" data-dir="${entry.dir}">${escapeHtml(entry.name)}</li>`);
    }
    $("browser-list").innerHTML = items.join("") || "<li>(empty)</li>";
  }
  $("browser-list").onclick = (e) => {
    const li = e.target.closest("li[data-path]");
    if (!li) return;
    if (li.dataset.dir === "false") {
      input.value = li.dataset.path;
      dialog.close();
    } else {
      show(li.dataset.path);
    }
  };
  $("browser-pick").onclick = () => { if (current) { input.value = current; dialog.close(); } };
  await show(input.value.trim());
  dialog.showModal();
}

async function openOther(path) {
  const status = $("other-status");
  if (!path) return setStatus(status, "Choose a folder or file first.", true);
  setStatus(status, "Looking…");
  try {
    const { job_id } = await post("/api/inspect", { path, mp3_decoder: $("set-mp3").value });
    const lib = await waitForJob(job_id, status);
    const entry = { format: lib.format, path: lib.path };
    const key = `${entry.format}:${entry.path}`;
    if (!state.sources.concat(state.extra).some((s) => `${s.format}:${s.path}` === key)) state.extra.push(entry);
    state.libs[key] = lib;
    state.tabs = buildTabs();
    setStatus(status, "");
    selectTab(key);
  } catch (err) {
    setStatus(status, err.message, true);
  }
}

async function uploadFile() {
  const file = $("upload").files[0];
  if (!file) return;
  const status = $("other-status");
  setStatus(status, `Uploading ${file.name}…`);
  const form = new FormData();
  form.append("file", file);
  try {
    const result = await api("/api/upload", { method: "POST", body: form });
    await openOther(result.path);
  } catch (err) {
    setStatus(status, `Upload failed: ${err.message}`, true);
  }
}

// --- setup ------------------------------------------------------------------------------

async function init() {
  $("tabs").addEventListener("click", (e) => {
    const tab = e.target.closest("[data-key]");
    if (tab) selectTab(tab.dataset.key);
  });
  $("view-actions").addEventListener("click", (e) => {
    const act = e.target.closest("[data-act]");
    if (!act) return;
    if (act.dataset.act === "reload") {
      delete state.libs[state.active];
      loadTab(activeTab()).catch(() => {});
    } else {
      openWizard(act.dataset.act);
    }
  });
  $("view-backups").addEventListener("click", (e) => {
    const btn = e.target.closest("[data-restore]");
    if (btn) restoreBackup(Number(btn.dataset.restore));
  });
  $("wiz-choices").addEventListener("click", (e) => {
    const btn = e.target.closest(".source");
    const w = state.wizard;
    if (!btn || !w) return;
    if (w.kind === "export") w.dst = w.choices.find((c) => c.id === btn.dataset.id);
    else w.src = btn.dataset.id;
    w.previewed = false;
    $("wiz-report").innerHTML = "";
    setStatus($("wiz-status"), "");
    if (w.kind === "export" && w.dst.format === "serato") $("wiz-serato-tags").checked = !w.dst.exists || Boolean(tabByKey(w.dst.tabKey).drive);
    renderWizard();
  });
  for (const id of ["wiz-both", "wiz-newformat"]) $(id).addEventListener("change", renderWizard);
  $("wiz-preview").addEventListener("click", () => transfer(true));
  $("wiz-run").addEventListener("click", () => transfer(false));
  const close = () => { state.wizard = null; renderWizard(); };
  $("wiz-cancel").addEventListener("click", close);
  $("cv-cancel").addEventListener("click", close);
  $("cv-run").addEventListener("click", runDriveConvert);

  $("tree").addEventListener("click", (e) => {
    const name = e.target.closest(".name");
    if (name) showTracks(name.dataset.path);
  });
  $("tree").addEventListener("change", (e) => {
    const box = e.target;
    if (box.type !== "checkbox") return;
    box.closest("li").querySelectorAll("input[type=checkbox]").forEach((b) => { b.checked = box.checked; });
    collectSelection();
  });
  $("tracks").addEventListener("click", (e) => {
    const row = e.target.closest("tr[data-id]");
    if (row) showTrack(row.dataset.id);
  });
  $("select-all").addEventListener("click", (e) => { e.preventDefault(); selectAll(true); });
  $("select-none").addEventListener("click", (e) => { e.preventDefault(); selectAll(false); });
  let searchTimer;
  $("track-search").addEventListener("input", () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => showTracks(state.currentPlaylist), 250);
  });

  $("other-browse").addEventListener("click", () => openBrowser($("other-path")));
  $("other-open").addEventListener("click", () => openOther($("other-path").value.trim()));
  $("upload").addEventListener("change", uploadFile);

  await refresh();
  watchDrives();
}

init().catch((err) => { document.querySelector("main").insertAdjacentHTML("afterbegin", `<p class="status error">Could not start: ${escapeHtml(err.message)}</p>`); });
