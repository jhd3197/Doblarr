import { state, library, titleState, castState, cfgGet } from './state.js';
import { PLAN_FIELDS } from './settings-model.js';
import { escapeHtml, el } from './dom.js';
import { castParams, jobsFor } from './identity.js';
import { api } from './api.js';
import { getJobs } from './jobs-data.js';

export function createTitle({ findItemByKey, setPage, queueDub, fetchPlan, statusTag, langChipsHtml, jobStatusTag, openWatch }) {
  const TITLE_TABS = [["plan", "Dub plan"], ["voices", "Speakers & voices"], ["jobs", "Jobs"], ["meta", "Metadata"]];

  // Per-title dub plan — real config keys, so the overrides genuinely reach the
  // pipeline (the worker deep-merges them over the global config for that job).
  // Shows come from Sonarr with a series folder; films from Radarr with a file.
  function isShow(item) { return !/^radarr/i.test(item.source || ""); }

  function titlePosterHtml(item) {
    return item.poster
      ? `<img class="poster-img" style="border-radius:10px;" src="${escapeHtml(item.poster)}" alt=""
              onerror="this.onerror=null;this.hidden=true;this.nextElementSibling.hidden=false">
         <div class="poster-fallback" hidden style="border-radius:10px;">${escapeHtml(item.title)}</div>`
      : `<div class="poster-fallback" style="border-radius:10px;">${escapeHtml(item.title)}</div>`;
  }

  function planValue(f) {
    if (titleState.plan && titleState.plan[f.k] !== undefined) return titleState.plan[f.k];
    if (f.dyn) return (library.targets && library.targets[0]) || "en";
    const c = cfgGet(f.k);
    return c === undefined || c === null ? "" : c;
  }

  function renderPlanField(f) {
    const over = !!(titleState.plan && titleState.plan[f.k] !== undefined);
    const v = planValue(f);
    const row = el("div", { class: "frow" });
    const left = el("div", {}, el("div", { class: "flabel" }, f.l));
    if (over) {
      left.append(el("div", { style: "margin-top: 5px;" },
        el("span", { class: "tag tag-accent" }, "Overridden")));
    } else {
      left.append(el("div", { style: "margin-top: 5px; font-size: 12px; color: var(--muted);" },
        "Inherited from Settings"));
    }
    row.append(left);

    const right = el("div", {});
    if (f.t === "text") {
      right.append(el("input", {
        class: "input m", type: "text", value: v, style: "max-width: 420px;",
        onchange: e => setPlanValue(f.k, e.target.value),
      }));
    } else {
      const opts = el("div", { class: "opts" });
      const options = f.t === "bool" ? ["On", "Off"] : (f.dyn ? (library.targets || ["en"]) : f.o);
      options.forEach(o => {
        const on = f.t === "bool" ? ((v ? "On" : "Off") === o) : (String(v) === o);
        opts.append(el("button", {
          type: "button", class: "opt", "aria-pressed": on ? "true" : "false",
          onclick: () => setPlanValue(f.k, f.t === "bool" ? o === "On" : o),
        }, o));
      });
      right.append(opts);
    }
    if (f.h) right.append(el("p", { class: "hint" }, f.h));
    row.append(right);
    return row;
  }

  function setPlanValue(key, value) {
    titleState.plan = { ...(titleState.plan || {}), [key]: value };
    renderTitle();   // reflect the override immediately
    savePlan();
  }

  async function savePlan() {
    const item = titleState.item;
    if (!item) return;
    titleState.planStatus = "Saving…";
    updatePlanStatus();
    try {
      const body = { title: item.title, plan: titleState.plan || {} };
      if (item.path) body.path = item.path;
      else if (item.tmdb_id) body.tmdb_id = item.tmdb_id;
      else if (item.tvdb_id) body.tvdb_id = item.tvdb_id;
      await api("api/plan", {
        method: "PUT",
        json: body,
      });
      titleState.planStatus = "Saved ✓";
    } catch (err) {
      titleState.planStatus = "Save failed: " + err.message;
    }
    updatePlanStatus();
  }

  function updatePlanStatus() {
    const n = document.getElementById("planStatus");
    if (n) n.textContent = titleState.planStatus;
  }

  function renderTitle() {
    const root = document.getElementById("titleRoot");
    if (!root) return;
    const item = titleState.item || findItemByKey(state.titleKey || "");
    if (!item) {
      root.innerHTML = library.loaded
        ? `<div class="panel" style="padding:22px 24px;color:var(--muted);">This title isn't in the current scan.
             <button type="button" class="btn btn-ghost" id="titleBack">← Library</button></div>`
        : `<div style="padding:22px 10px;color:var(--muted);">Loading…</div>`;
      const back = document.getElementById("titleBack");
      if (back) back.addEventListener("click", () => setPage("Library"));
      return;
    }
    titleState.item = item;
    const show = isShow(item);
    const target = (titleState.plan && titleState.plan.target_lang)
      || (library.targets && library.targets[0]) || "en";
    const langs = (item.audio_langs && item.audio_langs.length)
      ? item.audio_langs.join(" · ") : (item.existing_audio || item.original);
    const statHead = 'margin:0;font-size:11.5px;letter-spacing:0.06em;text-transform:uppercase;color:var(--muted);';
    const statVal = 'margin:3px 0 0;font-size:14px;font-weight:600;';
    root.innerHTML = `
      <div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap;">
        <button type="button" class="btn btn-ghost" id="titleBack">← Library</button>
        <span class="m" style="font-size:12.5px;color:var(--muted);user-select:all;word-break:break-all;">${escapeHtml(item.path || "")}</span>
        <span style="margin-left:auto;display:flex;gap:6px;">
          ${statusTag(item.label)}
          <span class="tag tag-neutral">${escapeHtml(item.source)}</span>
        </span>
      </div>
      <div class="panel" style="padding:20px;display:grid;grid-template-columns:180px minmax(0,1fr);gap:24px;">
        <div style="width:180px;">${titlePosterHtml(item)}</div>
        <div style="min-width:0;display:flex;flex-direction:column;gap:12px;">
          <div style="display:flex;align-items:flex-start;gap:14px;flex-wrap:wrap;">
            <div style="min-width:0;">
              <h2 style="font-size:27px;margin:0;letter-spacing:-0.015em;">${escapeHtml(item.title)}${item.year ? ` <span style="font-weight:500;color:var(--muted);">(${item.year})</span>` : ""}</h2>
              <p class="m" style="margin:5px 0 0;font-size:12.5px;color:var(--muted);">${show ? "Series" : "Film"} · ${escapeHtml(item.original)} · ${escapeHtml(langs)}</p>
            </div>
            <div style="display:flex;gap:8px;margin-left:auto;flex-wrap:wrap;">
              <button type="button" class="btn btn-secondary" id="tpTease">Preview a tease</button>
              <button type="button" class="btn btn-primary" id="tpQueue" style="color:#fff;">Queue dub</button>
            </div>
          </div>
          <p style="margin:0;font-size:12.5px;color:var(--muted);">${show
            ? "Queueing sends the series folder — every episode file under it gets the AI track."
            : "Queueing sends this film's file — the dub is muxed in as an extra audio track."}</p>
          <div style="display:flex;gap:26px;flex-wrap:wrap;padding-top:4px;">
            <div><p style="${statHead}">Dub direction</p><p class="m" style="${statVal}">${escapeHtml(item.original)} → ${escapeHtml(target)}</p></div>
            <div><p style="${statHead}">Audio tracks</p><p class="m" style="${statVal}">${escapeHtml(langs)}</p></div>
            <div><p style="${statHead}">Jobs</p><p class="m" id="tpStatJobs" style="${statVal}">—</p></div>
            <div><p style="${statHead}">Voices assigned</p><p class="m" id="tpStatVoices" style="${statVal}">—</p></div>
          </div>
        </div>
      </div>
      <div style="display:flex;gap:6px;flex-wrap:wrap;">
        ${TITLE_TABS.map(([k, t]) => `<button type="button" class="tab" data-dtab="${k}" aria-current="${titleState.dtab === k ? "page" : "false"}">${t}</button>`).join("")}
      </div>
      <div id="titleTabBody"></div>`;
    document.getElementById("titleBack").addEventListener("click", () => setPage("Library"));
    document.getElementById("tpQueue").addEventListener("click", e => queueDub(item, e.currentTarget, "full"));
    document.getElementById("tpTease").addEventListener("click", e => queueDub(item, e.currentTarget, "tease"));
    root.querySelectorAll("[data-dtab]").forEach(b =>
      b.addEventListener("click", () => { titleState.dtab = b.dataset.dtab; renderTitle(); }));
    // Hero stats + the plan load lazily; the plan re-renders once it arrives.
    if (!titleState.plan) {
      fetchPlan(item).then(p => {
        if (titleState.item !== item || state.page !== "Title") return;
        titleState.plan = p;
        renderTitle();
      }).catch(error => {
        if (titleState.item !== item) return;
        titleState.planStatus = "Could not load plan: " + error.message;
        updatePlanStatus();
      });
    }
    getJobs().then(data => {
      const n = document.getElementById("tpStatJobs");
      if (n && titleState.item === item) n.textContent = String(jobsFor(item, data.jobs || []).length);
    }).catch(() => {});
    api("api/cast?" + castParams(item)).then(data => {
      const cast = data.cast || [];
      const n = document.getElementById("tpStatVoices");
      if (n && titleState.item === item)
        n.textContent = cast.length ? `${cast.filter(e => e.voice).length} / ${cast.length}` : "0";
    }).catch(() => {});
    renderTitleTab();
  }

  function renderTitleTab() {
    const body = document.getElementById("titleTabBody");
    if (!body || !titleState.item) return;
    const item = titleState.item;
    if (titleState.dtab === "plan") {
      body.innerHTML = `
        <div class="panel" style="padding:20px 24px 22px;">
          <div style="display:flex;align-items:baseline;gap:12px;flex-wrap:wrap;">
            <h3 style="font-size:17px;margin:0;">Dub plan for this title</h3>
            <p style="margin:0;font-size:13px;color:var(--muted);">Anything you change here overrides your global settings for ${escapeHtml(item.title)} only.</p>
            <span id="planStatus" style="font-size:12.5px;color:var(--muted);">${escapeHtml(titleState.planStatus)}</span>
            <button type="button" class="btn btn-ghost" id="planReset" style="margin-left:auto;">Reset to global</button>
          </div>
          <div id="planFields" style="margin-top:10px;"></div>
        </div>`;
      document.getElementById("planReset").addEventListener("click", () => {
        titleState.plan = {};
        renderTitle();
        savePlan();
      });
      const box = document.getElementById("planFields");
      if (!titleState.plan) box.innerHTML = `<p style="color:var(--muted);font-size:13px;">Loading plan…</p>`;
      else box.replaceChildren(...PLAN_FIELDS.map(renderPlanField));
      return;
    }
    if (titleState.dtab === "voices") {
      body.innerHTML = `
        <div class="panel" style="padding:20px 24px 22px;">
          <div style="display:flex;align-items:baseline;gap:12px;flex-wrap:wrap;">
            <h3 style="font-size:17px;margin:0;">Speakers and voices</h3>
            <p style="margin:0;font-size:13px;color:var(--muted);">One voice per speaker — reused by teases and the full dub. Run a tease to auto-assign by archetype, then adjust here.</p>
          </div>
          <div id="titleCastRows" style="margin-top:12px;"></div>
          <div style="display:flex;gap:10px;justify-content:flex-end;margin-top:16px;align-items:center;">
            <span id="titleCastStatus" style="margin-right:auto;font-size:12.5px;color:var(--muted);"></span>
            <button type="button" class="btn btn-primary" id="titleCastSave">Save cast</button>
          </div>
        </div>`;
      document.getElementById("titleCastSave").addEventListener("click", saveCast);
      openCast(item);
      return;
    }
    if (titleState.dtab === "jobs") {
      body.innerHTML = `
        <div class="panel" style="padding:6px 18px 10px;">
          <div id="titleJobs"><p style="color:var(--muted);font-size:13px;padding:12px 4px;">Loading…</p></div>
        </div>`;
      getJobs().then(data => {
        if (state.page === "Title" && titleState.dtab === "jobs")
          renderTitleJobs(jobsFor(item, data.jobs || []));
      }).catch(() => {
        const n = document.getElementById("titleJobs");
        if (n) n.innerHTML = `<p style="color:var(--muted);font-size:13px;padding:12px 4px;">Jobs unavailable.</p>`;
      });
      return;
    }
    // meta
    const show = isShow(item);
    const langs = (item.audio_langs && item.audio_langs.length)
      ? item.audio_langs.join(" · ") : (item.existing_audio || item.original);
    const facts = [
      ["Type", show ? "Series (per-episode files)" : "Film (single file)"],
      ["Source", item.source],
      ["Year", item.year || "—"],
      ["Original language", item.original],
      ["Audio tracks present", langs],
      ["Target languages", (library.targets || []).join(", ") || "—"],
      ["Plex label", item.label],
      ["Auto-dub", item.auto_dub ? "On" : "Off"],
      ["Path", item.path || "—"],
      ["TMDB id", item.tmdb_id || "—"],
      ["TVDB id", item.tvdb_id || "—"],
    ];
    body.innerHTML = `
      <div style="display:flex;flex-direction:column;gap:14px;">
        <div class="panel" style="padding:20px 24px 22px;">
          <h3 style="font-size:17px;margin:0 0 2px;">Matched metadata</h3>
          <p style="margin:0 0 12px;font-size:13px;color:var(--muted);">Read from ${escapeHtml(item.source)} — used to pick the source language and the voice profile.</p>
          ${facts.map(([k, v]) => `
            <div class="frow" style="grid-template-columns:190px minmax(0,1fr);">
              <div class="flabel" style="font-weight:500;color:var(--muted);">${escapeHtml(k)}</div>
              <div class="m" style="font-size:13px;padding-top:8px;word-break:break-all;user-select:all;">${escapeHtml(String(v))}</div>
            </div>`).join("")}
        </div>
        <div class="panel" style="padding:20px 24px 22px;">
          <h3 style="font-size:17px;margin:0 0 10px;">Audio languages</h3>
          <div class="lang-chips">${langChipsHtml(item)}</div>
          <p style="margin:12px 0 0;font-size:12.5px;color:var(--muted);">Dashed chips are target languages with no audio track yet — that's what a dub adds.</p>
        </div>
      </div>`;
  }

  function renderTitleJobs(list) {
    const box = document.getElementById("titleJobs");
    if (!box) return;
    if (!list.length) {
      box.innerHTML = `<p style="color:var(--muted);font-size:13px;padding:12px 4px;">No jobs for this title yet.</p>`;
      return;
    }
    box.innerHTML = list.slice(0, 10).map(j => `
      <div style="display:flex;align-items:center;gap:10px;padding:9px 4px;border-bottom:1px solid var(--line);font-size:13px;">
        <span class="m" style="color:var(--muted);font-size:12px;">${escapeHtml((j.created_at || "").slice(5, 16))}</span>
        ${jobStatusTag(j.status)}${j.kind === "tease" ? ' <span class="tag tag-outline" style="font-size:10.5px;padding:1px 7px;">tease</span>' : ""}
        <span style="color:var(--muted);font-size:12.5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${escapeHtml(j.message || j.stage || "")}</span>
        ${j.has_file ? `<button type="button" class="btn btn-ghost detail-watch" data-id="${j.id}" style="margin-left:auto;">Watch</button>` : ""}
      </div>`).join("");
    box.querySelectorAll(".detail-watch").forEach(b =>
      b.addEventListener("click", () => openWatch(b.dataset.id, { jobs: list })));
  }

  // ---- Voice cast editor (per-title, shared by teases and full dubs) ----

  function renderCastRows() {
    const box = document.getElementById("titleCastRows");
    if (!box) return;
    if (!castState.cast.length) {
      box.innerHTML = `<p style="color:var(--muted);font-size:13px;">No cast yet — run a Tease to auto-assign voices by archetype, then adjust here.</p>`;
      return;
    }
    const voiceOpts = cur => [`<option value="">(unassigned)</option>`]
      .concat((castState.voices || []).map(v =>
        `<option value="${escapeHtml(v.id)}"${v.id === cur ? " selected" : ""}>${escapeHtml(v.name)}</option>`))
      .join("");
    box.innerHTML = castState.cast.map((e, i) => `
      <div style="display:flex;align-items:center;gap:10px;padding:8px 0;border-bottom:1px solid var(--line);">
        <span style="font-weight:600;min-width:100px;">${escapeHtml(e.label)}</span>
        <span class="lang-chip">${escapeHtml(e.category)}</span>
        <span class="m" style="font-size:11.5px;color:var(--muted);">${escapeHtml(e.speaker_id)}</span>
        <select class="input cast-voice" data-idx="${i}" style="margin-left:auto;max-width:210px;">${voiceOpts(e.voice)}</select>
      </div>`).join("");
    box.querySelectorAll(".cast-voice").forEach(sel =>
      sel.addEventListener("change", () => {
        castState.cast[Number(sel.dataset.idx)].voice = sel.value;
      }));
  }

  async function openCast(item) {
    castState.item = item;
    const status = document.getElementById("titleCastStatus");
    if (status) status.textContent = "";
    try {
      const fetches = [api("api/cast?" + castParams(item))];
      if (!castState.voices) fetches.push(api("api/voices"));
      const [cr, vr] = await Promise.all(fetches);
      castState.cast = cr.cast || [];
      if (vr) castState.voices = vr.voices || [];
    } catch (e) {
      castState.cast = [];
      if (status) status.textContent = "Cast unavailable — is the API reachable?";
    }
    renderCastRows();
    const stat = document.getElementById("tpStatVoices");
    if (stat && titleState.item === item)
      stat.textContent = castState.cast.length
        ? `${castState.cast.filter(e => e.voice).length} / ${castState.cast.length}` : "0";
  }

  async function saveCast() {
    const status = document.getElementById("titleCastStatus");
    const item = castState.item;
    if (!item || !status) return;
    const body = { title: item.title, cast: castState.cast };
    if (item.path) body.path = item.path;
    else if (item.tmdb_id) body.tmdb_id = item.tmdb_id;
    else if (item.tvdb_id) body.tvdb_id = item.tvdb_id;
    status.textContent = "Saving…";
    try {
      await api("api/cast", {
        method: "PUT",
        json: body,
      });
      status.textContent = "Saved ✓";
    } catch (err) {
      status.textContent = "Save failed: " + err.message;
    }
  }

  // ---- Jobs that share a library title (used by the title page) ----
  return { renderTitle, renderTitleJobs };
}
