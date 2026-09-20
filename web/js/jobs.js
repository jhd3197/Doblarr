import { state, library, jobs, matchesSearch } from './state.js';
import { escapeHtml, safeGet } from './dom.js';
import { api, apiUrl } from './api.js';
import { getJobs } from './jobs-data.js';

export function createJobs({ loadLibrary, goTitle, onTitleJobs, loadConfig }) {
  function jobStatusTag(s) {
    const cls = s === "running" ? "tag-accent"
              : s === "failed" || s === "cancelled" || s === "planned" ? "tag-outline" : "tag-neutral";
    return `<span class="tag ${cls}">${s}</span>`;
  }

  function updateDryRunTag() {
    const t = document.getElementById("dryRunTag");
    if (!t) return;
    t.hidden = !(state.config && state.config.dub && state.config.dub.dry_run);
  }

  function renderJobs(data) {
    jobs.lastData = data;
    const body = document.getElementById("dubsBody");
    const list = (data.jobs || []).filter(j => matchesSearch(j.title));
    const c = data.counts || {};

    // toolbar state
    const paused = !!data.paused;
    const pauseBtn = document.getElementById("pauseQueue");
    if (pauseBtn) pauseBtn.textContent = paused ? "Resume queue" : "Pause queue";
    const pausedTag = document.getElementById("pausedTag");
    if (pausedTag) pausedTag.hidden = !paused;
    const workerTag = document.getElementById("workerTag");
    if (workerTag) workerTag.textContent = (c.running || 0) ? `processing ${c.running}` : "worker idle";

    if (!list.length) {
      body.innerHTML = `<tr><td colspan="6" style="padding:22px 10px;color:var(--muted);">No jobs yet — queue one from the Library.</td></tr>`;
      return;
    }
    body.innerHTML = list.map(j => {
      const track = `${escapeHtml(j.source_lang)} → ${escapeHtml(j.target_lang)}`;
      const stage = j.status === "done" ? escapeHtml(j.message || "done")
                  : j.status === "failed" ? escapeHtml(j.message || "failed")
                  : j.status === "cancelled" ? escapeHtml(j.message || "cancelled")
                  : escapeHtml(j.message || j.stage || "queued");
      const barColor = j.status === "failed" || j.status === "cancelled" ? "background: var(--color-neutral-600);" : "";
      const pct = (j.status === "running" || j.status === "queued")
        ? `<div class="m" style="font-size:11px;color:var(--muted);margin-top:3px;">${j.progress || 0}%</div>` : "";
      const act = (j.status === "queued" || j.status === "running") ? "Cancel" : "Remove";
      const watch = j.has_file
        ? `<button type="button" class="btn btn-ghost job-watch" data-id="${j.id}">Watch</button>` : "";
      const review = j.has_review
        ? `<button type="button" class="btn btn-secondary job-review" data-id="${j.id}">Review${j.review_count ? ` (${j.review_count})` : ''}</button>` : '';
      const action = (act ? `<button type="button" class="btn btn-ghost job-del" data-id="${j.id}">${act}</button>` : "") + watch + review;
      const st = (j.status === "done" && (j.message || "").startsWith("planned")) ? "planned" : j.status;
      const version = j.version_id ? `<div class="m" style="font-size:11px;color:var(--muted);" title="${escapeHtml(j.version_id)}">${escapeHtml(j.version_name || 'Dub')} · ${escapeHtml(j.version_id.slice(0, 12))}<br>Script ${escapeHtml((j.translation_id || '').slice(0, 12))}</div>` : '';
      return `
        <tr>
          <td style="font-weight:600;">${escapeHtml(j.title)}${j.kind === "tease" ? ' <span class="tag tag-outline" style="font-size:10.5px;padding:1px 7px;">tease</span>' : ""}</td>
          <td class="m" style="font-size:13px;">${track}${version}</td>
          <td>${stage}</td>
          <td><div class="bar"><span style="width:${j.progress || 0}%;${barColor}"></span></div>${pct}</td>
          <td>${jobStatusTag(st)}</td>
          <td>${action}</td>
        </tr>`;
    }).join("");
  }

  async function loadJobs() {
    if (!state.config) loadConfig().then(updateDryRunTag); else updateDryRunTag();
    try {
      const data = await getJobs();
      renderJobs(data);
      setOverviewJobStats(data.counts || {});
      if (state.page === "Overview") {
        renderInProgress(data.jobs || []);
        renderActivity(data.jobs || []);
        renderRecentTitles(data.jobs || []);
      }
      onTitleJobs(data);
    } catch (e) {
      const body = document.getElementById("dubsBody");
      if (body) body.innerHTML = `<tr><td colspan="6" style="padding:22px 10px;color:var(--muted);">Jobs unavailable — is Doblarr running (doblarr serve)?</td></tr>`;
    }
  }

  function startJobPolling() {
    stopJobPolling();
    jobs.pollTimer = setInterval(loadJobs, 30000);  // slow fallback when SSE is down
  }
  function stopJobPolling() {
    if (jobs.pollTimer) { clearInterval(jobs.pollTimer); jobs.pollTimer = null; }
  }

  let refreshTimer;
  function scheduleJobRefresh() {
    if (refreshTimer) return;
    refreshTimer = setTimeout(() => { refreshTimer = null; loadJobs(); }, 100);
  }

  // Live updates via SSE (/api/events); falls back to slow polling if the stream
  // keeps failing. EventSource can't set headers, so the API key goes on the URL.
  function startEventStream() {
    stopEventStream();
    const key = safeGet("doblarr_api_key", "");
    const es = new EventSource(apiUrl("events") + (key ? "?api_key=" + encodeURIComponent(key) : ""));
    jobs.eventSource = es;
    es.onmessage = (e) => {
      jobs.eventFailures = 0;
      stopJobPolling();  // stream is healthy — no fallback needed
      let evt;
      try { evt = JSON.parse(e.data); } catch { return; }
      if (evt.topic === "log") { appendLogLine(evt); return; }
      if (evt.topic === "job") scheduleJobRefresh();
      else if (evt.topic === "scan" && state.page === "Overview") loadOverview();
    };
    es.onerror = () => {
      jobs.eventFailures = (jobs.eventFailures || 0) + 1;
      if (jobs.eventFailures >= 3) startJobPolling();
    };
  }
  function stopEventStream() {
    if (jobs.eventSource) { jobs.eventSource.close(); jobs.eventSource = null; }
  }
  window.addEventListener("doblarr-api-key", startEventStream);  // reconnect with the new key

  // ---- Live log panel (SSE topic: log) ----
  const logLines = [];
  function appendLogLine(evt) {
    const box = document.getElementById("logBox");
    if (!box) return;
    logLines.push(`${(evt.at || "").slice(11)} ${evt.level.padEnd(7)} ${evt.logger} | ${evt.message}`);
    if (logLines.length > 200) logLines.splice(0, logLines.length - 200);
    box.textContent = logLines.join("\n");
    box.scrollTop = box.scrollHeight;
  }

  function setOverviewJobStats(counts) {
    const r = document.getElementById("statRunning");
    const q = document.getElementById("statQueued");
    if (r) r.textContent = counts.running || 0;
    if (q) q.textContent = counts.queued || 0;
  }

  function renderInProgress(list) {
    const box = document.getElementById("inProgress");
    const active = list.filter(j => j.status === "running" || j.status === "queued");
    if (!active.length) {
      box.innerHTML = `<div class="panel" style="padding:18px 20px;color:var(--muted);">Nothing in progress — queue a dub from the Library.</div>`;
      return;
    }
    box.innerHTML = active.map(j => `
      <div class="panel" style="padding:18px 20px;">
        <div style="display:flex;align-items:baseline;gap:14px;flex-wrap:wrap;">
          <span style="font-family:var(--font-heading);font-weight:800;font-size:16px;">${escapeHtml(j.title)}</span>
          <span class="m" style="font-size:12.5px;color:var(--muted);">${escapeHtml(j.source_lang)} → ${escapeHtml(j.target_lang)}</span>
          <span class="tag ${j.status === "running" ? "tag-accent" : "tag-neutral"}" style="margin-left:auto;">${j.status === "running" ? escapeHtml(j.message || j.stage || "running") : "queued"}</span>
        </div>
        <div class="bar" style="margin-top:14px;"><span style="width:${j.progress || 0}%;"></span></div>
        <div class="m" style="font-size:11.5px;color:var(--muted);margin-top:6px;">${j.progress || 0}%${j.status === "running" && j.message ? " — " + escapeHtml(j.message) : ""}</div>
      </div>`).join("");
  }

  function renderActivity(list) {
    const body = document.getElementById("activityBody");
    const recent = [...list].sort((a, b) => (b.updated_at || "").localeCompare(a.updated_at || "")).slice(0, 6);
    if (!recent.length) {
      body.innerHTML = `<tr><td colspan="4" style="padding:18px 10px;color:var(--muted);">No activity yet.</td></tr>`;
      return;
    }
    const evt = { done: "Dub completed", failed: "Job failed", running: "Processing", queued: "Queued" };
    const cls = { done: "tag-neutral", failed: "tag-outline", running: "tag-accent", queued: "tag-neutral" };
    body.innerHTML = recent.map(j => `
      <tr>
        <td class="m" style="font-size:13px;">${escapeHtml((j.updated_at || "").slice(11, 16))}</td>
        <td>${escapeHtml(evt[j.status] || j.status)}</td>
        <td>${escapeHtml(j.title)}</td>
        <td><span class="tag ${cls[j.status] || "tag-neutral"}">${escapeHtml(j.status)}</span></td>
      </tr>`).join("");
  }

  // Titles touched recently (queued, teased, finished) as poster cards that
  // jump straight to their title page — no searching for the same show again.
  async function renderRecentTitles(jobList) {
    const box = document.getElementById("recentTitles");
    if (!box) return;
    const seen = new Set();
    const titles = [];
    for (const j of [...jobList].sort((a, b) => (b.updated_at || "").localeCompare(a.updated_at || ""))) {
      if (!seen.has(j.title)) { seen.add(j.title); titles.push(j.title); }
      if (titles.length >= 8) break;
    }
    if (!titles.length) {
      box.innerHTML = `<div style="color:var(--muted);font-size:13px;">Nothing yet — queue a dub from the Library.</div>`;
      return;
    }
    if (!library.loaded) await loadLibrary();
    box.innerHTML = "";
    titles.forEach(t => {
      const item = library.items.find(i => i.title === t);
      if (!item) return;
      const card = document.createElement("div");
      card.className = "poster-card";
      card.style.cursor = "pointer";
      card.innerHTML = (item.poster
        ? `<img class="poster-img" loading="lazy" src="${escapeHtml(item.poster)}" alt=""
                onerror="this.onerror=null;this.hidden=true;this.nextElementSibling.hidden=false">
           <div class="poster-fallback" hidden>${escapeHtml(item.title)}</div>`
        : `<div class="poster-fallback">${escapeHtml(item.title)}</div>`)
        + `<div class="poster-body"><div style="font-weight:600;font-size:12.5px;line-height:1.3;">${escapeHtml(item.title)}</div></div>`;
      card.addEventListener("click", () => goTitle(item));
      box.append(card);
    });
    if (!box.children.length)
      box.innerHTML = `<div style="color:var(--muted);font-size:13px;">Recent jobs are for titles no longer in the scan.</div>`;
  }

  async function loadOverview() {
    try {
      const d = await getJobs();
      setOverviewJobStats(d.counts || {});
      renderInProgress(d.jobs || []);
      renderActivity(d.jobs || []);
      renderRecentTitles(d.jobs || []);
    } catch (e) {}
    try {
      const st = await api("status");
      const nd = document.getElementById("statNeedsDub");
      if (nd && st.counts) nd.textContent = st.counts.needs_dub ?? "—";
    } catch (e) {}
    // If no scan has run yet, trigger one so the needs-dub count is populated.
    const nd = document.getElementById("statNeedsDub");
    if (nd && nd.textContent === "—") {
      try {
        const lr = await api("api/library");
        nd.textContent = (lr.counts || {}).needs_dub ?? "—";
      } catch (e) {}
    }
  }

  return { jobStatusTag, updateDryRunTag, renderJobs, loadJobs, loadOverview, startEventStream };
}
