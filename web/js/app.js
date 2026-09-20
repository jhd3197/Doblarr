import { state, library, jobs, titleState } from './state.js';
import { escapeHtml, safeGet, safeSet } from './dom.js';
import { api, apiUrl } from './api.js';
import { createSettings } from './settings.js';
import { createLibrary } from './library.js';
import { createJobs } from './jobs.js';
import { createTitle } from './title.js';
import { createReview } from './review.js';
import { jobsFor } from './identity.js';

const { statusTag, langChipsHtml, renderLibrary, fetchPlan, queueDub, loadLibrary } = createLibrary({ goTitle });
const { jobStatusTag, updateDryRunTag, renderJobs, loadJobs, loadOverview, startEventStream } = createJobs({
  loadLibrary, goTitle, loadConfig: () => loadConfig(),
  onTitleJobs: data => {
    if (state.page === "Title" && titleState.item && titleState.dtab === "jobs")
      renderTitleJobs(jobsFor(titleState.item, data.jobs || []));
  },
});
const { renderTitle: renderTitleView, renderTitleJobs } = createTitle({ goTitle, goEpisode, findItemByKey, setPage, queueDub, fetchPlan, statusTag, langChipsHtml, jobStatusTag, openWatch });
const { loadConfig, saveSettings, renderSettings } = createSettings({
  setPage, onConfigLoaded: () => { updateDryRunTag(); if (state.page === "Title") renderTitle(); },
});


const review = createReview({ onQueued: loadJobs });

// ---- App state + rendering (vanilla, no framework) ----
const app = document.getElementById("app");

function setTheme(theme) {
  app.setAttribute("data-theme", theme);
  document.querySelectorAll("#themebtn button").forEach(b =>
    b.setAttribute("aria-pressed", b.dataset.themeSet === theme ? "true" : "false"));
  safeSet("doblarr.theme", theme);
}

// ---- History API routing (/library, /settings/<tab>) — the server serves
// index.html for any extensionless non-api path, so refresh/deep-link works.
const PAGE_PATHS = { "/": "Overview", "/library": "Library", "/dubs": "Dubs", "/voices": "Voices", "/settings": "Settings", "/title": "Title" };
const PAGE_TO_PATH = { Overview: "/", Library: "/library", Dubs: "/dubs", Voices: "/voices", Settings: "/settings" };

function routeFromPath() {
  const parts = location.pathname.split("/").filter(Boolean);
  const page = PAGE_PATHS["/" + (parts[0] || "").toLowerCase()] || "Overview";  // unknown -> Overview
  return {
    page,
    tab: page === "Settings" ? (parts[1] || null) : null,
    // Only the first segment is lowercased — the title key keeps its case.
    titleKey: page === "Title" ? decodeURIComponent(parts[1] || "") : null,
    episodeId: page === "Title" && parts[2] === "episode" ? Number(parts[3]) : null,
  };
}

function setPage(page, tab) {
  let target = PAGE_TO_PATH[page] || "/";
  if (page === "Settings" && tab) target += "/" + tab;
  if (location.pathname !== target) history.pushState({}, "", target);
  applyRoute();
}

// Stable identity for a library item in the URL: an external id when the
// source provides one, else the title (+ year) as text.
function itemKey(item) {
  if (item.tmdb_id) return "tmdb-" + item.tmdb_id;
  if (item.tvdb_id) return "tvdb-" + item.tvdb_id;
  return "t-" + item.title + (item.year ? "-" + item.year : "");
}

function findItemByKey(key) {
  return library.items.find(i => itemKey(i) === key) || null;
}

function goTitle(item, dtab) {
  state.titleKey = itemKey(item);
  if (dtab) titleState.dtab = dtab;
  titleState.item = item;
  titleState.plan = null;  // re-fetched for the new title
  const target = "/title/" + encodeURIComponent(state.titleKey);
  if (location.pathname !== target) history.pushState({}, "", target);
  applyRoute();
}

function goEpisode(item, episode) {
  history.pushState({}, '', `/title/${encodeURIComponent(itemKey(item))}/episode/${episode.id}`);
  titleState.dtab = 'voices';
  applyRoute();
}

let episodeRequest = 0, episodeLoaded = '', episodePending = '';
function renderTitle() {
  if (state.page !== 'Title') return;
  const parent = findItemByKey(state.titleKey);
  if (!state.episodeId) {
    episodeRequest++; episodePending = ''; episodeLoaded = '';
    if (titleState.item !== parent) { titleState.item = parent; titleState.plan = null; }
    renderTitleView(); return;
  }
  if (!parent) { renderTitleView(); return; }
  const key = `${state.titleKey}:${state.episodeId}`;
  if (episodeLoaded === key) { renderTitleView(); return; }
  if (episodePending === key) return;
  episodePending = key;
  const request = ++episodeRequest;
  const episodeId = state.episodeId;
  document.getElementById('titleRoot').textContent = 'Loading episode…';
  api(`series/${parent.tvdb_id}/episodes?target_lang=${encodeURIComponent(library.targets?.[0] || 'en')}`).then(data => {
    if (request !== episodeRequest || state.page !== 'Title') return;
    const ep = data.episodes.find(e => e.id === episodeId);
    if (!ep) throw new Error('Episode not found in Sonarr');
    titleState.item = { ...parent, parent, media_type: 'episode', episode_id: ep.id,
      season: ep.season, episode_number: ep.episode, path: ep.path,
      title: `${parent.title} · S${String(ep.season).padStart(2,'0')}E${String(ep.episode).padStart(2,'0')} — ${ep.title}`,
      audio_langs: ep.audio_langs, label: ep.status, status: ep.status };
    titleState.plan = null; titleState.dtab = 'voices';
    episodeLoaded = key; episodePending = ''; renderTitleView();
  }).catch(error => { if (request === episodeRequest) {
    episodePending = ''; document.getElementById('titleRoot').textContent = error.message;
  } });
}

function applyRoute() {
  const { page, tab, titleKey, episodeId } = routeFromPath();
  state.page = page;
  document.title = "Doblarr — " + page;
  document.querySelectorAll("[data-view]").forEach(s =>
    s.hidden = s.getAttribute("data-view") !== page);
  document.querySelectorAll("#nav .navitem").forEach(n =>
    n.setAttribute("aria-current",
      (n.dataset.page === page || (page === "Title" && n.dataset.page === "Library")) ? "page" : "false"));
  if (page === "Settings") {
    if (tab) state.tab = tab;
    if (state.config) renderSettings(); else loadConfig();
  }
  if (page === "Title") {
    state.titleKey = titleKey;
    state.episodeId = episodeId;
    if (!state.config) loadConfig();  // inherited plan values come from here
    if (!library.loaded) loadLibrary().then(renderTitle); else renderTitle();
  }
  if (page === "Library" && !library.loaded) loadLibrary();
  if (page === "Overview") loadOverview();
  if (page === "Dubs") loadJobs();
}
window.addEventListener("popstate", applyRoute);  // back/forward

// ---- Library (real data from /api/library) ----

// ---- Jobs (real queue from /api/jobs) ----

// ---- Wire up events ----
document.getElementById("nav").addEventListener("click", e => {
  const btn = e.target.closest(".navitem"); if (btn) setPage(btn.dataset.page);
});
document.getElementById("themebtn").addEventListener("click", e => {
  const btn = e.target.closest("button"); if (btn) setTheme(btn.dataset.themeSet);
});
document.getElementById("toggleKeys").addEventListener("click", () => {
  state.keys = !state.keys; renderSettings();
});
// Single-select pill groups on the Library page
document.querySelectorAll(".opts[data-single]").forEach(group => {
  group.addEventListener("click", e => {
    const btn = e.target.closest(".opt"); if (!btn) return;
    group.querySelectorAll(".opt").forEach(o => o.setAttribute("aria-pressed", "false"));
    btn.setAttribute("aria-pressed", "true");
    if (btn.dataset.filter) { library.filter = btn.dataset.filter; renderLibrary(); }
    if (btn.dataset.sort) { library.sort = btn.dataset.sort; renderLibrary(); }
  });
});
document.getElementById("rescanBtn").addEventListener("click", loadLibrary);
document.getElementById("saveSettings").addEventListener("click", saveSettings);

async function syncPlexLabels(apply) {
  const s = document.getElementById("labelStatus");
  s.textContent = apply ? "Applying Plex labels…" : "Previewing…";
  try {
    const r = await api("api/plex/labels", {
      method: "POST",
      json: { apply },
    });
    const unm = (r.unmatched || []).length;
    if (apply) {
      s.innerHTML = `Labelled <b>${r.added}</b> as “${escapeHtml(r.label)}”, removed <b>${r.removed}</b> stale · matched ${r.matched}` +
        (unm ? ` · ${unm} not found in Plex` : "") +
        (r.kometa_file ? ` · Kometa fragment written` : "");
    } else {
      s.innerHTML = `Preview: would label <b>${r.added}</b> as “${escapeHtml(r.label)}” (matched ${r.matched}` +
        (unm ? `, ${unm} unmatched` : "") + `), remove <b>${r.removed}</b> stale. ` +
        `<button type="button" class="btn btn-primary" id="applyLabels" style="margin-left:8px;padding:5px 12px;">Apply</button>`;
      document.getElementById("applyLabels").addEventListener("click", () => syncPlexLabels(true));
    }
  } catch (err) {
    s.textContent = "Plex labels: " + err.message;
  }
}
document.getElementById("syncLabelsBtn").addEventListener("click", () => syncPlexLabels(false));
document.getElementById("libraryGrid").addEventListener("click", e => {
  const btn = e.target.closest(".queue-dub, .tease-dub, .cast-edit");
  if (btn) {
    const item = library.rendered[Number(btn.dataset.idx)];
    if (!item) return;
    if (btn.classList.contains("cast-edit")) goTitle(item, "voices");
    else queueDub(item, btn, btn.classList.contains("tease-dub") ? "tease" : "full");
    return;
  }
  const card = e.target.closest(".poster-card");
  if (card) {
    const item = library.rendered[Number(card.dataset.idx)];
    if (item) goTitle(item);
  }
});
document.getElementById("clearFinished").addEventListener("click", async () => {
  try { await api("api/jobs/clear-finished", { method: "POST" }); } catch (e) { window.alert(e.message); }
  loadJobs();
});
document.getElementById("pauseQueue").addEventListener("click", async () => {
  const resuming = document.getElementById("pauseQueue").textContent.startsWith("Resume");
  try { await api(resuming ? "api/queue/resume" : "api/queue/pause", { method: "POST" }); } catch (e) { window.alert(e.message); }
  loadJobs();
});
document.getElementById("dubsBody").addEventListener("click", async e => {
  const reviewBtn = e.target.closest('.job-review');
  if (reviewBtn) { review.open(reviewBtn.dataset.id); return; }
  const watchBtn = e.target.closest(".job-watch");
  if (watchBtn) {
    openWatch(watchBtn.dataset.id, jobs.lastData);
    return;
  }
  const btn = e.target.closest(".job-del"); if (!btn) return;
  btn.disabled = true;
  try { await api("api/jobs/" + btn.dataset.id, { method: "DELETE" }); } catch (err) { btn.disabled = false; window.alert(err.message); }
  loadJobs();
});

// ---- Watch modal (streams /api/jobs/{id}/file with Range support) ----
const watchModal = document.getElementById("watchModal");
const watchVideo = document.getElementById("watchVideo");

function openWatch(jobId, data) {
  const job = (data && data.jobs || []).find(j => j.id === jobId);
  if (!job) return;
  const key = safeGet("doblarr_api_key", "");
  watchVideo.src = apiUrl("jobs/" + jobId + "/file") + (key ? "?api_key=" + encodeURIComponent(key) : "");
  document.getElementById("watchTitle").textContent =
    `${job.title}${job.kind === "tease" ? " (tease)" : ""}`;
  document.getElementById("watchPath").textContent = job.output_file || "";
  watchModal.hidden = false;
}

function closeWatch() {
  watchVideo.pause();
  watchVideo.removeAttribute("src");  // stop the stream
  watchVideo.load();
  watchModal.hidden = true;
}
document.getElementById("watchClose").addEventListener("click", closeWatch);
watchModal.addEventListener("click", e => { if (e.target === watchModal) closeWatch(); });

// Global search: filters the current page's list by title.
document.getElementById("globalSearch").addEventListener("input", e => {
  state.search = e.target.value.trim().toLowerCase();
  if (state.page === "Library" && library.loaded) renderLibrary();
  else if (state.page === "Dubs" && jobs.lastData) renderJobs(jobs.lastData);
});

// New dub modal (manual enqueue).
const newDubModal = document.getElementById("newDubModal");
function openNewDub() {
  document.getElementById("ndTitle").value = "";
  document.getElementById("ndFrom").value = "auto";
  document.getElementById("ndTo").value = (library.targets && library.targets[0]) || "en";
  document.getElementById("ndPath").value = "";
  document.getElementById("ndStatus").textContent = "";
  newDubModal.hidden = false;
  document.getElementById("ndTitle").focus();
}
function closeNewDub() { newDubModal.hidden = true; }
document.getElementById("newDubBtn").addEventListener("click", openNewDub);
document.getElementById("ndCancel").addEventListener("click", closeNewDub);
newDubModal.addEventListener("click", e => { if (e.target === newDubModal) closeNewDub(); });
document.getElementById("ndQueue").addEventListener("click", async e => {
  const title = document.getElementById("ndTitle").value.trim();
  if (!title) { document.getElementById("ndTitle").focus(); return; }
  const button = e.currentTarget;
  button.disabled = true;
  document.getElementById("ndStatus").textContent = "";
  try {
    await api("jobs", {
      method: "POST",
      json: {
        title, source: "manual",
        source_lang: document.getElementById("ndFrom").value.trim() || "auto",
        target_lang: document.getElementById("ndTo").value.trim() || "en",
        path: document.getElementById("ndPath").value.trim() || null,
        kind: document.getElementById('ndKind').value,
      },
    });
    closeNewDub();
    setPage("Dubs");
  } catch (error) {
    document.getElementById("ndStatus").textContent = error.message;
  } finally {
    button.disabled = false;
  }
});

// ---- Title page (full-page item detail, modelled on the design demo) ----
// Esc closes whichever modal is open.
document.addEventListener("keydown", e => {
  if (e.key !== "Escape") return;
  if (!watchModal.hidden) closeWatch();
  else if (!newDubModal.hidden) closeNewDub();
});

// ---- Init ----
const savedTheme = safeGet("doblarr.theme", "light");
setTheme(savedTheme === "dark" ? "dark" : "light");
applyRoute();  // open the view named by the URL path
startEventStream();
