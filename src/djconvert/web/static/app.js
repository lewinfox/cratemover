"use strict";

const $ = (id) => document.getElementById(id);
const state = { info: null, library: null, selected: new Set(), currentPlaylist: "" };

const HINTS = {
  mixxx: "Mixxx's mixxxdb.sqlite, or the folder holding it (~/.mixxx on Linux).",
  rekordbox_xml: "In Rekordbox: File › Export Collection in xml format. Choose or upload that file.",
  rekordbox_db: "Rekordbox 6/7's own library: the rekordbox folder (~/Library/Pioneer/rekordbox on a Mac, " +
    "%APPDATA%\\Pioneer\\rekordbox on Windows) or its master.db. Beat grids come from its share/ folder.",
  serato: "The _Serato_ folder (~/Music/_Serato_, or the root of an external drive), or its parent. " +
    "Cue points and beat grids are read from the music files, so they must be reachable too.",
};

const NEXT_STEPS = {
  rekordbox_xml: `<h3>Importing into Rekordbox</h3><ol>
    <li>Rekordbox › Preferences › Advanced › Database › <i>rekordbox xml</i> › Imported Library: choose <code>rekordbox.xml</code>.</li>
    <li>It appears under <b>rekordbox xml</b> in the tree (enable it under Preferences › View › Layout if not).</li>
    <li>Right-click playlists there › <i>Import Playlist</i>. Tracks already in your collection keep their old cues unless you re-import them with <i>Import To Collection</i>.</li></ol>`,
  serato: `<h3>Installing into Serato</h3><ol>
    <li>Quit Serato. <b>Back up</b> your existing <code>_Serato_</code> folder.</li>
    <li>Copy the generated <code>database V2</code> and <code>Subcrates</code> into it (on a Mac: <code>~/Music/_Serato_</code>; for an external drive: <code>_Serato_</code> at the drive's root). Without a merge base, the database replaces your existing one.</li>
    <li>Start Serato. Cue points, loops and beat grids come from the files' tags, if you chose to write them.</li></ol>`,
  mixxx: `<h3>Using it in Mixxx</h3><ol>
    <li>Quit Mixxx. <b>Back up</b> your existing <code>mixxxdb.sqlite</code> (~/.mixxx on Linux, ~/Library/Application Support/Mixxx on macOS, %LOCALAPPDATA%\\Mixxx on Windows).</li>
    <li>Replace it with the generated <code>mixxxdb.sqlite</code> (merged into your existing library if you gave one).</li>
    <li>Start Mixxx. Add your music folder under Preferences › Library if it is not there.</li></ol>`,
};

async function api(path, options = {}) {
  const response = await fetch(path, options);
  if (!response.ok) {
    let detail = response.statusText;
    try { detail = (await response.json()).detail || detail; } catch (_) { /* not JSON */ }
    throw new Error(detail);
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

function showFor(attr, value) {
  document.querySelectorAll(`[${attr}]`).forEach((el) => {
    el.classList.toggle("hidden", !el.getAttribute(attr).split(" ").includes(value));
  });
}

function stat(label, value) {
  return `<div class="stat"><b>${escapeHtml(value)}</b><span>${escapeHtml(label)}</span></div>`;
}

// --- setup ------------------------------------------------------------------------------

async function init() {
  state.info = await api("/api/info");
  const options = (formats) => Object.entries(formats)
    .map(([k, v]) => `<option value="${k}">${escapeHtml(v)}</option>`).join("");
  $("src-format").innerHTML = options(state.info.source_formats);
  $("dst-format").innerHTML = options(state.info.target_formats);
  $("dst-format").value = "rekordbox_xml";
  $("src-format").addEventListener("change", onSourceFormat);
  $("dst-format").addEventListener("change", () => showFor("data-show-dst", $("dst-format").value));
  onSourceFormat();
  showFor("data-show-dst", $("dst-format").value);

  $("suggestions").innerHTML = state.info.suggestions.map((s) =>
    `<button type="button" class="chip" data-format="${s.format}" data-path="${escapeHtml(s.path)}">
      ${escapeHtml(state.info.formats[s.format])}: ${escapeHtml(s.path)}</button>`).join("");
  $("suggestions").addEventListener("click", (e) => {
    const chip = e.target.closest(".chip");
    if (!chip) return;
    $("src-format").value = chip.dataset.format;
    $("src-path").value = chip.dataset.path;
    onSourceFormat();
  });

  $("load-btn").addEventListener("click", loadLibrary);
  $("convert-btn").addEventListener("click", convert);
  $("browse-btn").addEventListener("click", () => openBrowser($("src-path").value));
  $("upload").addEventListener("change", uploadFile);
  $("select-all").addEventListener("click", (e) => { e.preventDefault(); selectAll(true); });
  $("select-none").addEventListener("click", (e) => { e.preventDefault(); selectAll(false); });
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

function onSourceFormat() {
  const format = $("src-format").value;
  $("src-hint").textContent = HINTS[format];
  showFor("data-show", format);
}

// --- file browser -----------------------------------------------------------------------

async function openBrowser(start) {
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
      $("src-path").value = li.dataset.path;
      dialog.close();
    } else {
      show(li.dataset.path);
    }
  };
  $("browser-pick").onclick = () => { if (current) { $("src-path").value = current; dialog.close(); } };
  const startDir = start && !start.match(/\.(xml|sqlite)$|database V2$/) ? start : start.replace(/\/[^/]*$/, "");
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
    $("src-path").value = result.path;
    if (/\.xml$/i.test(file.name)) $("src-format").value = "rekordbox_xml";
    else if (/\.sqlite$/i.test(file.name)) $("src-format").value = "mixxx";
    else if (/\.zip$/i.test(file.name)) $("src-format").value = "serato";
    onSourceFormat();
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
    const { job_id } = await post("/api/inspect", {
      format: $("src-format").value,
      path: $("src-path").value.trim(),
      access_rules: $("access-rules").value,
      mp3_decoder: $("src-mp3").value,
      serato_root: $("src-serato-root").value.trim() || "/",
      read_file_tags: $("src-read-tags").checked,
    });
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
    const li = box.closest("li");
    li.querySelectorAll("input[type=checkbox]").forEach((b) => { b.checked = box.checked; });
    collectSelection();
  };
  $("folders").innerHTML = lib.folders.map((f) =>
    `<button type="button" class="chip" data-folder="${escapeHtml(f.folder)}">${escapeHtml(f.folder)} (${f.tracks})</button>`).join("");
  $("library-card").classList.remove("hidden");
  $("target-card").classList.remove("hidden");
  $("result-card").classList.add("hidden");
  const src = $("src-format").value;
  if ($("dst-format").value === src) {
    $("dst-format").value = Object.keys(state.info.target_formats).find((f) => f !== src);
    showFor("data-show-dst", $("dst-format").value);
  }
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

async function convert() {
  const status = $("convert-status");
  const button = $("convert-btn");
  const format = $("dst-format").value;
  if (format === "serato" && $("dst-serato-tags").checked &&
      !confirm("This writes Serato cue and grid tags into your audio files. Continue?")) return;
  button.disabled = true;
  setStatus(status, "Converting…");
  try {
    const { job_id } = await post("/api/convert", {
      library_id: state.library.library_id,
      format,
      output_name: $("dst-name").value.trim() || "converted",
      path_rules: $("path-rules").value,
      key_notation: $("dst-key").value,
      mp3_decoder: format === "mixxx" ? $("dst-mp3").value : $("src-mp3").value,
      memory_cues_to_hot_cues: $("dst-mem-to-hot").checked,
      rekordbox_memory_cues: $("dst-rb-memory").checked,
      rekordbox_hot_cues_as_memory: $("dst-rb-hot-as-memory").checked,
      serato_root: $("dst-serato-root").value.trim() || "/",
      serato_write_tags: $("dst-serato-tags").checked,
      serato_max_hot_cues: Number($("dst-serato-cues").value),
      serato_base_database: $("dst-serato-base").value.trim(),
      mixxx_base_database: $("dst-mixxx-base").value.trim(),
      mixxx_playlists_as_crates: $("dst-mixxx-crates").checked,
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
  const outName = result.output_dir.split("/").pop();
  $("result-files").innerHTML = `<p>Written to <code>${escapeHtml(result.output_dir)}</code>:</p><div class="files">` +
    result.files.map((f) => `<a href="/api/download?path=${encodeURIComponent(f)}">${escapeHtml(f)}</a>`).join("") +
    `<a href="/api/download?path=${encodeURIComponent(outName)}"><b>Download all (.zip)</b></a></div>`;
  $("result-warnings").innerHTML = result.warnings.map((w) => `<li>${escapeHtml(w)}</li>`).join("");
  $("next-steps").innerHTML = NEXT_STEPS[result.format] || "";
  $("result-card").classList.remove("hidden");
  $("result-card").scrollIntoView({ behavior: "smooth" });
}

init().catch((err) => setStatus($("load-status"), `Could not start: ${err.message}`, true));
