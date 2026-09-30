"use strict";

const $ = (id) => document.getElementById(id);
const state = { info: null, library: null, selected: new Set(), currentPlaylist: "", pickers: {} };

const HINTS = {
  mixxx: "Mixxx's mixxxdb.sqlite, or the folder holding it (~/.mixxx on Linux).",
  rekordbox_usb: "A Rekordbox USB stick: its mount point (the folder holding PIONEER/). Cues, grids and waveforms come from its analysis files.",
  rekordbox_xml: "In Rekordbox: File › Export Collection in xml format. Choose or upload that file.",
  rekordbox_db: "Rekordbox 6/7's own library: the rekordbox folder (~/Library/Pioneer/rekordbox on a Mac, " +
    "%APPDATA%\\Pioneer\\rekordbox on Windows) or its master.db. Beat grids come from its share/ folder.",
  serato: "A _Serato_ folder (~/Music/_Serato_, or at the root of a USB stick), or the folder holding it. " +
    "Cue points and beat grids are read from the music files, so they must be reachable too.",
};

const NEXT_STEPS = {
  rekordbox_xml: `<h3>Importing into Rekordbox</h3><ol>
    <li>Rekordbox › Preferences › Advanced › Database › <i>rekordbox xml</i> › Imported Library: choose <code>rekordbox.xml</code>.</li>
    <li>It appears under <b>rekordbox xml</b> in the tree (enable it under Preferences › View › Layout if not).</li>
    <li>Right-click playlists there › <i>Import Playlist</i>.</li></ol>`,
  rekordbox_usb: `<h3>Using the stick</h3><ol>
    <li>Eject it safely, then plug it into the laptop (Rekordbox shows it under Devices; Mixxx lists it under Rekordbox) or into a player. Pre-NXS2 players only play MP3 reliably.</li>
    <li>If Rekordbox ever loses playlist entries on it (one report says Rekordbox 7 did this to another tool's stick), write it again: it's quick, and the old <code>export.pdb</code> is kept as a backup.</li>
    <li>If something looks wrong, the previous <code>export.pdb</code> is kept next to the new one with a <code>.djconvert-…</code> suffix.</li></ol>`,
  serato: `<h3>Using it in Serato</h3><ol>
    <li>Quit Serato. For a new library, copy the generated <code>database V2</code> and <code>Subcrates</code> into your <code>_Serato_</code> folder (back it up first).</li>
    <li>Updated in place? Just start Serato.</li>
    <li>Cue points, loops and beat grids come from the files' tags, if you chose to write them.</li></ol>`,
  mixxx: `<h3>Using it in Mixxx</h3><ol>
    <li>Updated in place? Start Mixxx; a backup of the old database sits next to it.</li>
    <li>New library: quit Mixxx, back up <code>mixxxdb.sqlite</code> and replace it with the generated one.</li></ol>`,
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

async function waitForJob(jobId, statusEl) {
  for (;;) {
    const job = await api(`/api/jobs/${jobId}`);
    if (job.status === "done") return job.result;
    if (job.status === "error") throw new Error(job.error);
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

function showFor(root, attr, value) {
  root.querySelectorAll(`[${attr}]`).forEach((el) => {
    el.classList.toggle("hidden", !el.getAttribute(attr).split(" ").includes(value));
  });
}

function stat(label, value) {
  return `<div class="stat"><b>${escapeHtml(value)}</b><span>${escapeHtml(label)}</span></div>`;
}

const options = (formats) => Object.entries(formats)
  .map(([k, v]) => `<option value="${k}">${escapeHtml(v)}</option>`).join("");

// --- library pickers -----------------------------------------------------------------------

function makePicker(container, formats) {
  container.appendChild($("picker-template").content.cloneNode(true));
  const field = (name) => container.querySelector(`[data-field="${name}"]`);
  field("format").innerHTML = options(formats);
  const update = () => {
    field("hint").textContent = HINTS[field("format").value] || "";
    showFor(container, "data-show-format", field("format").value);
  };
  field("format").addEventListener("change", update);
  container.querySelector("[data-browse]").addEventListener("click", () => openBrowser(field("path")));
  update();
  return {
    field,
    set(entry) {
      field("format").value = entry.format;
      field("path").value = entry.path;
      if (entry.serato_root) field("serato_root").value = entry.serato_root;
      update();
    },
    value() {
      return {
        format: field("format").value,
        path: field("path").value.trim(),
        access_rules: field("access_rules").value,
        mp3_decoder: field("mp3_decoder").value,
        serato_root: field("serato_root").value.trim() || "/",
        read_file_tags: field("read_file_tags").checked,
      };
    },
  };
}

function suggestionChips(el, onPick) {
  el.innerHTML = state.info.suggestions.map((s, i) =>
    `<button type="button" class="chip" data-i="${i}">${escapeHtml(state.info.formats[s.format])}: ${escapeHtml(s.path)}</button>`).join("");
  el.onclick = (e) => {
    const chip = e.target.closest(".chip");
    if (chip) onPick(state.info.suggestions[Number(chip.dataset.i)]);
  };
}

// --- setup ------------------------------------------------------------------------------

async function init() {
  state.info = await api("/api/info");
  state.pickers.src = makePicker($("src-picker"), state.info.source_formats);
  state.pickers.a = makePicker($("sync-a"), state.info.source_formats);
  state.pickers.b = makePicker($("sync-b"), state.info.source_formats);
  $("dst-format").innerHTML = options(state.info.target_formats);
  $("dst-format").value = "rekordbox_usb";

  suggestionChips($("suggestions"), (s) => state.pickers.src.set(s));
  let next = "a";
  suggestionChips($("sync-suggestions"), (s) => {
    state.pickers[next].set(s);
    next = next === "a" ? "b" : "a";
  });

  document.querySelectorAll("[data-tab]").forEach((tab) => tab.addEventListener("click", () => switchTab(tab.dataset.tab)));

  $("dst-format").addEventListener("change", updateTarget);
  document.querySelectorAll("input[name=dst-mode]").forEach((r) => r.addEventListener("change", updateTarget));
  document.querySelectorAll("button[data-browse]").forEach((b) => {
    if (b.dataset.browse) b.addEventListener("click", () => openBrowser($(b.dataset.browse)));
  });
  updateTarget();

  watchDrives();
  $("load-btn").addEventListener("click", loadLibrary);
  $("convert-btn").addEventListener("click", convert);
  $("upload").addEventListener("change", uploadFile);
  $("select-all").addEventListener("click", (e) => { e.preventDefault(); selectAll(true); });
  $("select-none").addEventListener("click", (e) => { e.preventDefault(); selectAll(false); });
  $("sync-preview").addEventListener("click", () => runSync(true));
  $("sync-run").addEventListener("click", () => runSync(false));
  let searchTimer;
  $("track-search").addEventListener("input", () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => showTracks(state.currentPlaylist), 250);
  });
  $("folders").addEventListener("click", (e) => {
    const chip = e.target.closest(".chip");
    if (!chip) return;
    const rules = $("path-rules");
    rules.value = (rules.value.trim() ? rules.value.trim() + "\n" : "") + `${chip.dataset.folder} => `;
    rules.focus();
  });
}

function targetMode() {
  if ($("dst-format").value === "rekordbox_usb") return "in_place";
  return document.querySelector("input[name=dst-mode]:checked").value;
}

function updateTarget() {
  const format = $("dst-format").value;
  const mode = targetMode();
  showFor($("target-card"), "data-show-dst", format);
  showFor($("target-card"), "data-show-mode", mode);
  $("dst-mode-row").classList.toggle("hidden", format === "rekordbox_usb");
  $("dst-name-label").classList.toggle("hidden", mode !== "new");
  $("dst-path-label").classList.toggle("hidden", mode === "new");
  $("dst-path-caption").textContent = format === "rekordbox_usb" ? "USB stick (mount point) or folder"
    : { mixxx: "mixxxdb.sqlite to update", serato: "_Serato_ folder to update (or the drive holding it)",
        rekordbox_xml: "rekordbox.xml to overwrite" }[format];
}

// --- USB drives (hot-plug) -------------------------------------------------------------

const FORMAT_SHORT = { rekordbox_usb: "Rekordbox", serato: "Serato" };

function gb(bytes) {
  return bytes >= 1e9 ? `${(bytes / 1e9).toFixed(1)} GB` : `${Math.round(bytes / 1e6)} MB`;
}

async function watchDrives() {
  await refreshBackups();
  let known = null;
  let signature = "";
  const tick = async () => {
    try {
      const { drives, roots } = await api("/api/drives");
      const paths = drives.map((d) => d.path);
      if (known !== null) {
        const added = drives.filter((d) => !known.includes(d.path));
        const removed = known.filter((p) => !paths.includes(p));
        if (added.length) $("drives-status").textContent = `Connected: ${added.map((d) => d.label).join(", ")}`;
        else if (removed.length) $("drives-status").textContent = `Removed: ${removed.map((p) => p.split("/").pop()).join(", ")}`;
      }
      const now = drives.map((d) => `${d.path}:${d.libraries.map((l) => l.format).join(",")}:${d.writable}`).join("\n");
      if (now !== signature || !$("drives").childElementCount) {
        renderDrives(drives, roots, known === null ? [] : paths.filter((p) => !known.includes(p)));
        signature = now;
      }
      known = paths;
    } catch (_) {
      $("drives-status").textContent = "Can't list drives right now.";
    }
    setTimeout(tick, 3000);
  };
  tick();
}

const drivePanels = {};  // drive path -> { target, status, error, result }
let lastDrives = { drives: [], roots: [] };
let backupsCache = [];

async function refreshBackups() {
  try { backupsCache = (await api("/api/backups")).backups; } catch (_) { backupsCache = []; }
}

function convertPanel(d, i) {
  const panel = drivePanels[d.path];
  if (!panel) return "";
  const name = FORMAT_SHORT[panel.target];
  const source = d.libraries.map((l) => FORMAT_SHORT[l.format]).join(" + ");
  const used = d.total_bytes - d.free_bytes;
  if (panel.result) {
    const r = panel.result;
    return `<div class="panel"><b>Converted to ${escapeHtml(name)}.</b> Backup: <code>${escapeHtml(r.backup)}</code>
      ${r.removed.length ? `<br>Removed the old ${escapeHtml(r.removed.join(", "))} library.` : ""}
      <ul class="warnings">${r.warnings.map((w) => `<li>${escapeHtml(w)}</li>`).join("")}</ul>
      <button type="button" class="chip" data-act="close" data-i="${i}">Close</button></div>`;
  }
  return `<div class="panel">
    <b>Convert ${escapeHtml(d.label)} (${escapeHtml(source)}) to ${escapeHtml(name)}</b>
    <p class="hint">The ${escapeHtml(name)} library is written onto this drive, using the audio already on it. A backup comes first.</p>
    <label class="check"><input type="checkbox" data-opt="keep" checked> Keep the ${escapeHtml(source)} library too (the drive then works in both)</label>
    <label class="check"><input type="checkbox" data-opt="full"> Back up the whole drive (${gb(used)}), not just the library folders</label>
    ${panel.target === "serato" ? '<label class="check"><input type="checkbox" data-opt="tags" checked> Write cues and grids into the audio files (their previous Serato tags are backed up)</label>' : ""}
    ${panel.target === "rekordbox_usb" ? `<label>Where rekordbox's computer sees this drive (for <code>rekordbox.xml</code>)
      <input data-opt="xmlroot" placeholder="${escapeHtml(d.path)} — or E:/ on Windows, /Volumes/${escapeHtml(d.label)} on a Mac"></label>
      <label>OneLibrary (rekordbox 7, newest players)
      <select data-opt="onelibrary"><option value="auto">If the drive already has one</option><option value="on">Yes</option><option value="off">No</option></select></label>` : ""}
    <div class="actions"><button type="button" data-act="run" data-i="${i}" ${panel.status ? "disabled" : ""}>Back up and convert</button>
      <button type="button" class="secondary" data-act="close" data-i="${i}">Cancel</button></div>
    <div class="status${panel.error ? " error" : ""}">${escapeHtml(panel.error || panel.status || "")}</div>
  </div>`;
}

function backupsFor(d, i) {
  const mine = backupsCache.filter((b) => b.drive === d.path || b.label === d.label);
  if (!mine.length) return "";
  return `<details><summary>${mine.length} backup(s)</summary><ul class="backups">${mine.map((b, k) =>
    `<li>${escapeHtml(b.created)} · ${b.full ? "whole drive" : "library folders"} · ${gb(b.size_bytes)}
      <button type="button" class="chip" data-act="restore" data-i="${i}" data-k="${k}">Restore</button></li>`).join("")}</ul></details>`;
}

function renderDrives(drives, roots, fresh) {
  lastDrives = { drives, roots };
  if (!drives.length) {
    $("drives").innerHTML = `<p class="hint">No USB drives mounted under ${escapeHtml(roots.join(", "))}. Plug one in; it appears here within a few seconds.</p>`;
    return;
  }
  $("drives").innerHTML = drives.map((d, i) => {
    const formats = d.libraries.map((l) => l.format);
    const libs = d.libraries.map((l, j) => `<span class="pill">${escapeHtml(FORMAT_SHORT[l.format] || l.format)}</span>
      <button type="button" class="chip" data-act="open" data-i="${i}" data-j="${j}">Open</button>
      <button type="button" class="chip" data-act="a" data-i="${i}" data-j="${j}">Sync A</button>
      <button type="button" class="chip" data-act="b" data-i="${i}" data-j="${j}">Sync B</button>`).join(" ");
    const convert = formats.length ? Object.keys(FORMAT_SHORT).map((t) =>
      `<button type="button" data-act="convert" data-target="${t}" data-i="${i}" ${formats.length === 1 && formats[0] === t ? "disabled" : ""}>
        Convert drive to ${FORMAT_SHORT[t]}</button>`).join(" ") : "";
    return `<div class="drive${fresh.includes(d.path) ? " fresh" : ""}">
      <div><b>${escapeHtml(d.label)}</b> <span class="muted">${escapeHtml(d.path)} · ${escapeHtml(d.fstype || "?")} · ${gb(d.free_bytes)} free of ${gb(d.total_bytes)}</span></div>
      <div class="chips">${libs || '<span class="muted">No DJ library yet.</span>'}</div>
      ${convert ? `<div class="actions">${convert}</div>` : ""}
      ${convertPanel(d, i)}
      <div class="chips"><span class="muted">Export a loaded library here:</span>
        <button type="button" class="chip" data-act="rekordbox" data-i="${i}">Rekordbox</button>
        <button type="button" class="chip" data-act="serato" data-i="${i}">Serato</button></div>
      ${d.notes.map((n) => `<div class="note">${escapeHtml(n)}</div>`).join("")}
      ${backupsFor(d, i)}
    </div>`;
  }).join("");
  $("drives").onclick = onDriveClick;
}

function rerenderDrives() {
  renderDrives(lastDrives.drives, lastDrives.roots, []);
}

async function onDriveClick(e) {
  const btn = e.target.closest("[data-act]");
  if (!btn) return;
  const drives = lastDrives.drives;
  const drive = drives[Number(btn.dataset.i)];
  const lib = btn.dataset.j !== undefined ? drive.libraries[Number(btn.dataset.j)] : null;
  const act = btn.dataset.act;
  if (act === "open") { switchTab("convert"); state.pickers.src.set(lib); $("load-btn").focus(); }
  else if (act === "a" || act === "b") { switchTab("sync"); state.pickers[act].set(lib); }
  else if (act === "convert") { drivePanels[drive.path] = { target: btn.dataset.target }; rerenderDrives(); }
  else if (act === "close") { delete drivePanels[drive.path]; rerenderDrives(); }
  else if (act === "run") { runDriveConvert(drive, btn.closest(".panel")); }
  else if (act === "restore") {
    const mine = backupsCache.filter((b) => b.drive === drive.path || b.label === drive.label);
    const backup = mine[Number(btn.dataset.k)];
    if (!confirm(`Restore ${drive.label} to how it was at ${backup.created}? Library changes since then are undone.`)) return;
    btn.disabled = true;
    try {
      const { job_id } = await post("/api/backups/restore", { backup: backup.path, path: drive.path });
      const result = await waitForJob(job_id, $("drives-status"));
      $("drives-status").textContent = `Restored ${drive.label}: ${result.done.join(", ")}.`;
    } catch (err) {
      $("drives-status").textContent = `Restore failed: ${err.message}`;
    }
  } else {
    switchTab("convert");
    $("dst-format").value = act === "rekordbox" ? "rekordbox_usb" : "serato";
    if (act === "serato") {
      document.querySelector("input[name=dst-mode][value=in_place]").checked = true;
      $("dst-serato-root").value = drive.path;
    }
    $("dst-path").value = drive.path;
    updateTarget();
    $("target-card").classList.contains("hidden")
      ? setStatus($("load-status"), `Target set to ${drive.label}. Load a source library first.`)
      : $("target-card").scrollIntoView({ behavior: "smooth" });
  }
}

async function runDriveConvert(drive, panelEl) {
  const panel = drivePanels[drive.path];
  const opt = (name) => panelEl.querySelector(`[data-opt="${name}"]`);
  const body = {
    path: drive.path,
    targets: [panel.target],
    source_format: drive.libraries.map((l) => l.format).find((f) => f !== panel.target) || "",
    keep_source: opt("keep").checked,
    full_backup: opt("full").checked,
    serato_write_tags: opt("tags") ? opt("tags").checked : true,
    onelibrary: opt("onelibrary") ? opt("onelibrary").value : "auto",
    xml_root: opt("xmlroot") ? opt("xmlroot").value.trim() : "",
  };
  panel.status = "Starting…";
  panel.error = null;
  rerenderDrives();
  const statusEl = { set textContent(t) { panel.status = t; const el = document.querySelector(".panel .status"); if (el) el.textContent = t; } };
  try {
    const { job_id } = await post("/api/drives/convert", body);
    panel.result = await waitForJob(job_id, statusEl);
  } catch (err) {
    panel.error = err.message;
    panel.status = null;
  }
  await refreshBackups();
  rerenderDrives();
}

function switchTab(name) {
  document.querySelectorAll("[data-tab]").forEach((t) => t.setAttribute("aria-selected", String(t.dataset.tab === name)));
  $("tab-convert").classList.toggle("hidden", name !== "convert");
  $("tab-sync").classList.toggle("hidden", name !== "sync");
}

// --- file browser -----------------------------------------------------------------------

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
  const start = input.value;
  const startDir = start && !/\.(xml|sqlite|db|pdb)$|database V2$/.test(start) ? start : start.replace(/\/[^/]*$/, "");
  await show(startDir || "");
  dialog.showModal();
}

async function uploadFile() {
  const file = $("upload").files[0];
  if (!file) return;
  const status = $("load-status");
  setStatus(status, `Uploading ${file.name}…`);
  const form = new FormData();
  form.append("file", file);
  try {
    const result = await api("/api/upload", { method: "POST", body: form });
    const format = /\.xml$/i.test(file.name) ? "rekordbox_xml" : /\.sqlite$/i.test(file.name) ? "mixxx" : "serato";
    state.pickers.src.set({ format, path: result.path });
    setStatus(status, `Uploaded ${file.name}.`);
  } catch (err) {
    setStatus(status, `Upload failed: ${err.message}`, true);
  }
}

// --- load -------------------------------------------------------------------------------

async function loadLibrary() {
  const status = $("load-status");
  const button = $("load-btn");
  button.disabled = true;
  setStatus(status, "Loading…");
  try {
    const { job_id } = await post("/api/inspect", state.pickers.src.value());
    const result = await waitForJob(job_id, status);
    state.library = result;
    setStatus(status, `Loaded ${result.source}.`);
    renderLibrary(result);
  } catch (err) {
    setStatus(status, err.message, true);
  } finally {
    button.disabled = false;
  }
}

function renderLibrary(lib) {
  const s = lib.summary;
  $("stats").innerHTML = stat("tracks", s.tracks) + stat("playlists & crates", s.playlists) +
    stat("hot cues", s.hot_cues) + stat("memory cues & loops", s.memory_cues) +
    stat("with beat grid", s.gridded) + stat("files not found", s.missing_files);
  const warnings = [...lib.warnings];
  if (s.missing_files) {
    warnings.push(`${s.missing_files} file(s) not found here, e.g. ${lib.missing_examples.join(", ")}. ` +
      "Mount the music folder or add a file access rule.");
  }
  $("lib-warnings").innerHTML = warnings.map((w) => `<li>${escapeHtml(w)}</li>`).join("");
  state.selected = new Set();
  $("tree").innerHTML = lib.playlists.length ? renderTree(lib.playlists) : '<p class="hint">No playlists.</p>';
  $("tree").onclick = (e) => {
    const name = e.target.closest(".name");
    if (name) showTracks(name.dataset.path);
  };
  $("tree").onchange = (e) => {
    const box = e.target;
    if (box.type !== "checkbox") return;
    box.closest("li").querySelectorAll("input[type=checkbox]").forEach((b) => { b.checked = box.checked; });
    collectSelection();
  };
  $("folders").innerHTML = lib.folders.map((f) =>
    `<button type="button" class="chip" data-folder="${escapeHtml(f.folder)}">${escapeHtml(f.folder)} (${f.tracks})</button>`).join("");
  $("library-card").classList.remove("hidden");
  $("target-card").classList.remove("hidden");
  $("result-card").classList.add("hidden");
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
}

function selectAll(on) {
  $("tree").querySelectorAll("input[type=checkbox]").forEach((b) => { b.checked = on; });
  collectSelection();
}

async function showTracks(playlist) {
  state.currentPlaylist = playlist;
  $("tracks-title").textContent = playlist || "All tracks";
  $("track-detail").classList.add("hidden");
  const q = $("track-search").value.trim();
  const data = await api(`/api/libraries/${state.library.library_id}/tracks?playlist=${encodeURIComponent(playlist)}&q=${encodeURIComponent(q)}`);
  $("tracks").innerHTML = data.tracks.map((t) =>
    `<tr data-id="${escapeHtml(t.id)}" title="${escapeHtml(t.location)}"><td>${escapeHtml(t.artist)}</td><td>${escapeHtml(t.title)}</td>
     <td>${t.bpm || ""}</td><td>${t.hot_cues || ""}</td><td>${t.memory_cues || ""}</td><td>${t.grid || ""}</td></tr>`).join("");
  $("tracks-more").textContent = data.total > data.tracks.length ? `Showing ${data.tracks.length} of ${data.total}.` : `${data.total} track(s).`;
  $("tracks").onclick = async (e) => {
    const row = e.target.closest("tr[data-id]");
    if (!row) return;
    const track = await api(`/api/libraries/${state.library.library_id}/tracks/${encodeURIComponent(row.dataset.id)}`);
    const pane = $("track-detail");
    pane.textContent = formatTrack(track);
    pane.classList.remove("hidden");
  };
}

function formatTrack(t) {
  const lines = [`${t.artist} - ${t.title}`, t.location, `BPM ${t.bpm}  key ${t.key || "-"}`];
  if (t.grid.length) lines.push("Grid: " + t.grid.map((g) => `${(g.position_ms / 1000).toFixed(3)}s @ ${g.bpm.toFixed(2)} (beat ${g.beat})`).join(", "));
  for (const c of [...t.cues].sort((a, b) => a.position_ms - b.position_ms)) {
    const slot = c.slot === null ? "memory" : `hot ${String.fromCharCode(65 + c.slot)}`;
    const end = c.end_ms !== null ? `–${(c.end_ms / 1000).toFixed(3)}s` : "";
    const colour = c.colour !== null ? ` #${c.colour.toString(16).padStart(6, "0")}` : "";
    lines.push(`  ${c.role.padEnd(6)} ${slot.padEnd(7)} ${(c.position_ms / 1000).toFixed(3)}s${end} ${c.name || ""}${colour}`);
  }
  return lines.join("\n");
}

// --- convert ----------------------------------------------------------------------------

function writeOptions(format) {
  return {
    format,
    key_notation: $("dst-key").value,
    mp3_decoder: format === "mixxx" ? $("dst-mp3").value : state.pickers.src.value().mp3_decoder,
    memory_cues_to_hot_cues: $("dst-mem-to-hot").checked,
    rekordbox_memory_cues: $("dst-rb-memory").checked,
    rekordbox_hot_cues_as_memory: $("dst-rb-hot-as-memory").checked,
    serato_root: $("dst-serato-root").value.trim() || "/",
    serato_write_tags: $("dst-serato-tags").checked,
    serato_max_hot_cues: Number($("dst-serato-cues").value),
    serato_base_database: $("dst-serato-base").value.trim(),
    mixxx_base_database: $("dst-mixxx-base").value.trim(),
    mixxx_playlists_as_crates: $("dst-mixxx-crates").checked,
    copy_missing: format === "serato" ? $("dst-serato-copy").checked : $("dst-copy").checked,
    waveforms: $("dst-waveforms").checked,
    device_name: $("dst-device").value.trim(),
    onelibrary: $("dst-onelibrary").value,
    usb_xml_root: $("dst-xml-root").value.trim(),
  };
}

async function convert() {
  const status = $("convert-status");
  const button = $("convert-btn");
  const format = $("dst-format").value;
  const mode = targetMode();
  if (format === "serato" && $("dst-serato-tags").checked &&
      !confirm("This writes Serato cue and grid tags into your audio files. Continue?")) return;
  if (mode === "in_place" && format !== "rekordbox_usb" &&
      !confirm("This updates the library in place (a backup is made first). Is the DJ software closed?")) return;
  button.disabled = true;
  setStatus(status, "Converting…");
  try {
    const { job_id } = await post("/api/convert", {
      ...writeOptions(format),
      library_id: state.library.library_id,
      output_name: $("dst-name").value.trim() || "converted",
      path_rules: $("path-rules").value,
      in_place: mode === "in_place",
      target_path: $("dst-path").value.trim(),
      playlists: [...state.selected],
    });
    const result = await waitForJob(job_id, status);
    setStatus(status, "Done.");
    renderResult(result);
  } catch (err) {
    setStatus(status, err.message, true);
  } finally {
    button.disabled = false;
  }
}

function renderResult(result) {
  const s = result.summary;
  $("result-stats").innerHTML = stat("tracks", s.tracks) + stat("playlists", s.playlists) +
    stat("hot cues", s.hot_cues) + stat("memory cues & loops", s.memory_cues) + stat("with beat grid", s.gridded);
  const files = result.files.map((f) => f.download
    ? `<a href="/api/download?path=${encodeURIComponent(f.download)}">${escapeHtml(f.download)}</a>`
    : `<code>${escapeHtml(f.path)}</code>`).join(" ");
  const zip = result.zip ? `<a href="/api/download?path=${encodeURIComponent(result.zip)}"><b>Download all (.zip)</b></a>` : "";
  $("result-files").innerHTML = `<p>Written to <code>${escapeHtml(result.output_dir)}</code>:</p><div class="files">${files} ${zip}</div>`;
  $("result-warnings").innerHTML = result.warnings.map((w) => `<li>${escapeHtml(w)}</li>`).join("");
  $("next-steps").innerHTML = NEXT_STEPS[result.format] || "";
  $("result-card").classList.remove("hidden");
  $("result-card").scrollIntoView({ behavior: "smooth" });
}

// --- sync -------------------------------------------------------------------------------

async function runSync(dryRun) {
  const status = $("sync-status");
  const a = state.pickers.a.value();
  const b = state.pickers.b.value();
  if (!a.path || !b.path) { setStatus(status, "Choose both libraries.", true); return; }
  if (!dryRun && !confirm("Sync updates the libraries in place (each changed file is backed up first). Is the DJ software closed?")) return;
  for (const btn of [$("sync-preview"), $("sync-run")]) btn.disabled = true;
  setStatus(status, dryRun ? "Comparing…" : "Syncing…");
  try {
    const { job_id } = await post("/api/sync", {
      a, b,
      direction: $("sync-direction").value,
      prefer: $("sync-prefer").value,
      cues: $("sync-cues").value,
      grids: $("sync-grids").value,
      metadata: $("sync-metadata").value,
      playlists: $("sync-playlists").value,
      add_tracks: $("sync-add").checked,
      path_rules: $("sync-rules").value,
      dry_run: dryRun,
      write: {
        format: "mixxx",
        serato_write_tags: $("sync-serato-tags").checked,
        waveforms: $("sync-waveforms").checked,
        onelibrary: $("sync-onelibrary").value,
      },
    });
    const result = await waitForJob(job_id, status);
    setStatus(status, dryRun ? "Preview ready — nothing was changed." : "Done.");
    renderSync(result);
  } catch (err) {
    setStatus(status, err.message, true);
  } finally {
    for (const btn of [$("sync-preview"), $("sync-run")]) btn.disabled = false;
  }
}

function renderSync(result) {
  const parts = [];
  for (const [key, label] of [["a_to_b", "A → B"], ["b_to_a", "B → A"]]) {
    const side = result[key];
    if (!side) continue;
    const r = side.report;
    const by = Object.entries(r.matched_by).map(([k, v]) => `${v} by ${k}`).join(", ");
    parts.push(`<h3>${label}</h3><div class="stats">${stat("matched", r.matched)}${stat(result.dry_run ? "would add" : "added", r.added)}` +
      `${stat(result.dry_run ? "would update" : "updated", r.updated)}${stat("new playlists", r.playlists_added)}` +
      `${stat("changed playlists", r.playlists_updated)}</div>` +
      (by ? `<p class="hint">Matched ${escapeHtml(by)}.</p>` : "") +
      (r.details.length ? `<details><summary>${r.details.length} change(s)</summary><ul>${r.details.map((d) => `<li>${escapeHtml(d)}</li>`).join("")}</ul></details>` : "") +
      (side.written ? `<ul class="warnings">${side.written.warnings.map((w) => `<li>${escapeHtml(w)}</li>`).join("")}</ul>` : ""));
  }
  $("sync-result-title").textContent = result.dry_run ? "Preview" : "Result";
  $("sync-report").innerHTML = parts.join("");
  $("sync-result").classList.remove("hidden");
  $("sync-result").scrollIntoView({ behavior: "smooth" });
}

init().catch((err) => setStatus($("load-status"), `Could not start: ${err.message}`, true));
