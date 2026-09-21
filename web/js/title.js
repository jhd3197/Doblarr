import { state, library, titleState, castState, cfgGet } from './state.js';
import { PLAN_FIELDS } from './settings-model.js';
import { escapeHtml, el } from './dom.js';
import { castParams, jobsFor } from './identity.js';
import { api } from './api.js';
import { pickVoice } from './voice-picker.js';
import { renderEpisodes } from './episodes.js';
import { getJobs } from './jobs-data.js';
import { renderRecipes } from './recipes.js';

const ROLE_LABELS = {speaker:'Unknown speaker', narrator:'Narrator', child_f:'Girl', child_m:'Boy',
  young_f:'Young woman', young_m:'Young man', adult_f:'Adult woman', adult_m:'Adult man',
  elderly_f:'Older woman', elderly_m:'Older man'};

export function createTitle({ goTitle, goEpisode, goTitleTab, findItemByKey, setPage, queueDub, fetchPlan, statusTag, langChipsHtml, jobStatusTag, openWatch }) {
  const TITLE_TABS = [["plan", "Dub plan"], ["voices", "Speakers & voices"], ["jobs", "Jobs"], ["meta", "Metadata"], ["recipes", "Recipes"]];

  // Per-title dub plan — real config keys, so the overrides genuinely reach the
  // pipeline (the worker deep-merges them over the global config for that job).
  // Shows come from Sonarr with a series folder; films from Radarr with a file.
  function isShow(item) { return item.media_type === "show" || (!item.media_type && /^sonarr/i.test(item.source || "")); }
  let shownItem;
  let recipeView;

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
    if (f.t === "json") {
      const input = el("textarea", { class: "input m", rows: 4, "aria-label": f.l,
        onchange: e => {
          try {
            const value = JSON.parse(e.target.value);
            if (!value || Array.isArray(value) || typeof value !== 'object' || Object.values(value).some(x => typeof x !== 'string')) throw new Error();
            setPlanValue(f.k, value);
          } catch { titleState.planStatus = 'Enter a JSON object with text values.'; updatePlanStatus(); }
        } });
      input.value = JSON.stringify(v || {}, null, 2); right.append(input);
    } else if (f.t === "text") {
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
      if (item.episode_id && !item.path) body.key = `episode:${item.tvdb_id}:${item.episode_id}`;
      else if (item.path) body.path = item.path;
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
    if (shownItem !== item) {
      shownItem = item; titleState.castItem = null;
    }
    const target = (titleState.plan && titleState.plan.target_lang)
      || (library.targets && library.targets[0]) || "en";
    const langs = (item.audio_langs && item.audio_langs.length)
      ? item.audio_langs.join(" · ") : (item.existing_audio || item.original);
    const statHead = 'margin:0;font-size:11.5px;letter-spacing:0.06em;text-transform:uppercase;color:var(--muted);';
    const statVal = 'margin:3px 0 0;font-size:14px;font-weight:600;';
    root.innerHTML = `
      <div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap;">
        <button type="button" class="btn btn-ghost" id="titleBack">← ${item.parent ? escapeHtml(item.parent.title) : "Library"}</button>
        <span class="m" style="font-size:12.5px;color:var(--muted);user-select:all;word-break:break-all;">${escapeHtml(item.path || "")}</span>
        <span style="margin-left:auto;display:flex;gap:6px;">
          ${show ? '<span class="tag tag-accent">TV Show</span>' : `<span class="tag tag-accent">${item.parent ? 'Episode' : 'Movie'}</span>` + statusTag(item.label)}
          <span class="tag tag-neutral">${escapeHtml(item.source)}</span>
        </span>
      </div>
      <div class="panel title-hero">
        <div class="title-poster">${titlePosterHtml(item)}</div>
        <div style="min-width:0;display:flex;flex-direction:column;gap:12px;">
          <div style="display:flex;align-items:flex-start;gap:14px;flex-wrap:wrap;">
            <div style="min-width:0;">
              <h2 style="font-size:27px;margin:0;letter-spacing:-0.015em;">${escapeHtml(item.title)}${item.year ? ` <span style="font-weight:500;color:var(--muted);">(${item.year})</span>` : ""}</h2>
              <p class="m" style="margin:5px 0 0;font-size:12.5px;color:var(--muted);">${show ? "TV Show" : item.parent ? "Episode" : "Movie"} · ${escapeHtml(item.original)} · ${escapeHtml(langs)}</p>
            </div>
            <div style="display:flex;gap:8px;margin-left:auto;flex-wrap:wrap;">
              ${!show ? '<button type="button" class="btn btn-secondary" id="tpTease">Preview a tease</button>' : ""}
              ${!show ? '<button type="button" class="btn btn-secondary" id="tpAudition">Audition voices</button>' : ""}
              <button type="button" class="btn btn-primary" id="tpQueue" style="color:#fff;">${show ? "Choose episodes" : "Queue dub"}</button>
            </div>
          </div>
          <p style="margin:0;font-size:12.5px;color:var(--muted);">${show
            ? "Choose episodes below. Each downloaded file gets its own job; missing episodes are never queued."
            : `Queueing processes this ${item.parent ? "episode" : "movie"} file and writes an output with an extra audio track.`}</p>
          <div style="display:flex;gap:26px;flex-wrap:wrap;padding-top:4px;">
            <div><p style="${statHead}">Dub direction</p><p class="m" style="${statVal}">${escapeHtml(item.original)} → ${escapeHtml(target)}</p></div>
            <div><p style="${statHead}">Audio tracks</p><p class="m" style="${statVal}">${escapeHtml(langs)}</p></div>
            <div><p style="${statHead}">Jobs</p><p class="m" id="tpStatJobs" style="${statVal}">—</p></div>
            <div><p style="${statHead}">Voices assigned</p><p class="m" id="tpStatVoices" style="${statVal}">—</p></div>
          </div>
        </div>
      </div>
      <div style="display:flex;gap:6px;flex-wrap:wrap;">
        ${(show ? [["episodes", "Episodes"], ...TITLE_TABS.filter(([key]) => key !== "recipes")] : TITLE_TABS).map(([k, t]) => `<button type="button" class="tab" data-dtab="${k}" aria-current="${titleState.dtab === k ? "page" : "false"}">${t}</button>`).join("")}
      </div>
      <div id="titleTabBody"></div>`;
    document.getElementById("titleBack").addEventListener("click", () => item.parent ? goTitle(item.parent, "episodes") : setPage("Library"));
    document.getElementById("tpQueue").addEventListener("click", e => { if (show) { goTitleTab("episodes"); } else queueDub(item, e.currentTarget, "full"); });
    document.getElementById("tpTease")?.addEventListener("click", e => queueDub(item, e.currentTarget, "tease"));
    document.getElementById("tpAudition")?.addEventListener("click", e => queueDub(item, e.currentTarget, "audition"));
    if (item.parent && !item.path) {
      ['tpQueue', 'tpTease', 'tpAudition'].forEach(id => { const button = document.getElementById(id); if (button) { button.disabled = true; button.title = 'Download this episode in Sonarr first'; } });
    }
    root.querySelectorAll("[data-dtab]").forEach(b =>
      b.addEventListener("click", () => goTitleTab(b.dataset.dtab)));
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
    if (titleState.dtab !== "recipes" || recipeView?.item !== item) recipeView = null;
    if (titleState.dtab === "recipes") {
      if (!titleState.plan) {
        body.textContent = 'Loading saved recipe settings…';
        return;
      }
      const target = titleState.plan.target_lang || library.targets?.[0] || 'en';
      // Background config refreshes must not discard an import or unsaved notes.
      if (recipeView?.target === target) {
        body.replaceWith(recipeView.body);
        return;
      }
      renderRecipes(body, { item, target,
        onApplied: plan => { if (titleState.item === item) { titleState.plan = plan; titleState.castItem = null; } } });
      recipeView = { item, target, body };
      return;
    }
    if (titleState.dtab === "episodes" && isShow(item)) {
      body.className = 'panel episode-panel';
      const target = titleState.plan?.target_lang || library.targets?.[0] || 'en';
      renderEpisodes(body, { item, target, targets: library.targets || ['en'],
        onTarget: value => setPlanValue('target_lang', value),
        onCast: episode => goEpisode(item, episode),
        openWatch });
      return;
    }
    body.className = '';
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
            <p style="margin:0;font-size:13px;color:var(--muted);">Choose a narrator below, or edit voices discovered by an audition. Speaker labels identify this episode only; they do not prove character identity.</p>
          </div>
          <div id="narratorControls"></div><p id="castScope" class="hint"></p><div id="titleCastRows" style="margin-top:12px;"></div>
          <div style="display:flex;gap:10px;justify-content:flex-end;margin-top:16px;align-items:center;">
            <span id="titleCastStatus" style="margin-right:auto;font-size:12.5px;color:var(--muted);"></span>
            <button type="button" class="btn btn-primary" id="titleCastSave">Save cast</button>
          </div>
        </div>`;
      document.getElementById("titleCastSave").addEventListener("click", saveCast);
      renderNarrator(item);
      const castItem = titleState.castItem || item;
      document.getElementById('castScope').textContent = isShow(item)
        ? (titleState.castItem ? `Character cast: ${castItem.title}` : 'Choose Voices on an episode to edit its discovered cast. Narrator defaults apply to new jobs throughout this show.')
        : item.parent ? `Character cast: S${String(item.season).padStart(2,'0')}E${String(item.episode_number).padStart(2,'0')} · saved for this episode` : 'Character cast for this movie';
      openCast(castItem);
      return;
    }
    if (titleState.dtab === "jobs") {
      body.innerHTML = `
        <div class="panel" style="padding:6px 18px 10px;">
          <div id="titleJobs"><p style="color:var(--muted);font-size:13px;padding:12px 4px;">Loading…</p></div>
        </div>`;
      getJobs().then(data => {
        if (state.page === "Title" && titleState.item === item && titleState.dtab === "jobs")
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
      ["Type", item.episode_id ? "Episode (single file)" : show ? "Series (per-episode files)" : "Film (single file)"],
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

  async function renderNarrator(item) {
    const root = document.getElementById('narratorControls');
    const value = key => titleState.plan?.[key] ?? cfgGet(key) ?? '';
    root.innerHTML = `<div class="narrator-panel"><h3>Narrator voice</h3>
      <p class="hint">Used for a single narrator or a speaker marked Narrator. An explicit character voice takes priority. Multiple detected speakers keep their own voices.</p>
      <div class="narrator-fields"><label>Voice<select class="input" id="narratorVoice" aria-label="Voice"><option value="">Clone from the original audio</option></select></label>
      <label>Voice engine<select class="input" id="narratorEngine" aria-label="Voice engine">${['chatterbox', 'qwen', 'qwen_custom_voice', 'kokoro'].map(e => `<option value="${e}">${e}</option>`).join('')}</select></label>
      <label class="narrator-direction">Delivery direction<input class="input" id="narratorDelivery" maxlength="500" placeholder="Warm, calm storytelling with gentle pauses" value="${escapeHtml(value('dub.narrator_delivery'))}"></label></div>
      <p class="hint">Delivery direction requires Qwen and a compatible voice. Engine selection applies to this title's new jobs. Create or clone additional voices in Voicebox, then refresh this list.</p>
      <div class="episode-actions"><button class="btn btn-primary" id="narratorSave">Save narrator</button><button class="btn btn-secondary" id="narratorBrowse">Browse all voices & samples</button><button class="btn btn-ghost" id="narratorRefresh">Refresh voices</button><span id="narratorStatus" role="status"></span></div></div>`;
    const engine = root.querySelector('#narratorEngine');
    const currentEngine = value('voicebox.default_engine');
    if (![...engine.options].some(o => o.value === currentEngine)) engine.add(new Option(currentEngine, currentEngine));
    engine.value = currentEngine;
    const field = root.querySelector('#narratorVoice');
    const current = value('dub.narrator_voice');
    if (current) { field.add(new Option(current, current)); field.value = current; }
    const status = root.querySelector('#narratorStatus');
    root.querySelector('#narratorSave').onclick = async () => {
      const delivery = root.querySelector('#narratorDelivery').value.trim();
      if (delivery && !['qwen', 'qwen_custom_voice'].includes(engine.value)) {
        status.textContent = 'Choose a Qwen engine to use delivery direction, or leave it empty.'; return;
      }
      titleState.plan = { ...titleState.plan, 'dub.narrator_voice': field.value,
        'dub.narrator_delivery': delivery, 'voicebox.default_engine': engine.value };
      status.textContent = 'Saving…'; await savePlan();
      if (root.isConnected) status.textContent = titleState.planStatus;
    };
    async function refresh() {
      try {
        const result = await api('voices');
        if (!root.isConnected || titleState.item !== item) return;
        const chosen = field.value;
        field.replaceChildren(new Option('Clone from the original audio', ''));
        (result.voices || []).forEach(v => field.add(new Option(v.name, v.id)));
        if (chosen && ![...field.options].some(o => o.value === chosen)) field.add(new Option(`${chosen} (not currently listed)`, chosen));
        field.value = chosen;
        status.textContent = result.warning || (!result.voices?.length ? 'No saved voices found. Create a voice in Voicebox first.' : '');
      } catch(e) { if (root.isConnected) status.textContent = e.message; }
    }
    root.querySelector('#narratorBrowse').onclick = () => pickVoice({ language: titleState.plan?.target_lang || library.targets?.[0] || 'en', onSelect: voice => {
      if (!root.isConnected) return;
      if (![...field.options].some(o => o.value === voice.profile_id)) field.add(new Option(voice.name, voice.profile_id));
      field.value=voice.profile_id;
      if (![...engine.options].some(o => o.value === voice.engine)) engine.add(new Option(voice.engine,voice.engine));
      engine.value=voice.engine;
      root.querySelector('#narratorDelivery').value=voice.direction || '';
      status.textContent='Voice selected. Save narrator to apply it to new jobs.';
    } });
    root.querySelector('#narratorRefresh').onclick = refresh;
    refresh();
  }

  function renderCastRows() {
    const box = document.getElementById("titleCastRows");
    if (!box) return;
    if (!castState.cast.length) {
      box.innerHTML = `<p style="color:var(--muted);font-size:13px;">No speakers discovered for this file yet. Assign a narrator above, or run an episode audition to discover its speakers.</p>`;
      return;
    }
    const voiceOpts = cur => [`<option value="">Use narrator default / clone original</option>`,
      cur && !(castState.voices || []).some(v => v.id === cur) ? `<option value="${escapeHtml(cur)}" selected>${escapeHtml(cur)} (not currently listed)</option>` : '']
      .concat((castState.voices || []).map(v =>
        `<option value="${escapeHtml(v.id)}"${v.id === cur ? " selected" : ""}>${escapeHtml(v.name)}</option>`))
      .join("");
    box.innerHTML = castState.cast.map((e, i) => `
      <div style="display:flex;align-items:center;gap:10px;padding:8px 0;border-bottom:1px solid var(--line);">
        <label class="cast-name-field">Character name<input class="input cast-name" data-idx="${i}" value="${escapeHtml(e.label)}"></label>
        <label>Role<select class="input cast-category" data-idx="${i}">${['speaker', 'narrator', 'child_f', 'child_m', 'young_f', 'young_m', 'adult_f', 'adult_m', 'elderly_f', 'elderly_m'].map(c => `<option value="${c}" ${c === e.category ? 'selected' : ''}>${escapeHtml(ROLE_LABELS[c] || c)}</option>`).join('')}</select></label>
        <span class="m" style="font-size:11.5px;color:var(--muted);">${escapeHtml(e.speaker_id)}</span>
        <label>Voice<select class="input cast-voice" data-idx="${i}">${voiceOpts(e.voice)}</select></label>
        <button class="btn btn-secondary cast-browse" data-idx="${i}">Find matching voice</button><label>Delivery (Qwen)<input class="input cast-delivery" data-idx="${i}" maxlength="500" value="${escapeHtml(e.delivery || '')}"></label>
      </div>`).join("");
    box.querySelectorAll(".cast-voice").forEach(sel =>
      sel.addEventListener("change", () => {
        const entry=castState.cast[Number(sel.dataset.idx)];
        entry.voice = sel.value;
        entry.engine = (castState.voices || []).find(v=>v.id===sel.value)?.engine || '';
        if (entry.engine && !['qwen','qwen_custom_voice'].includes(entry.engine)) entry.delivery='';
      }));
    box.querySelectorAll('.cast-browse').forEach(button => button.onclick = () => {
      const entry=castState.cast[Number(button.dataset.idx)];
      pickVoice({language:titleState.plan?.target_lang || library.targets?.[0] || 'en',category:entry.category,onSelect:voice=>{
        entry.voice=voice.profile_id;entry.engine=voice.engine;
        entry.delivery=voice.direction || '';
        if(!(castState.voices || []).some(v=>v.id===voice.profile_id)) (castState.voices ||= []).push({id:voice.profile_id,name:voice.name});
        renderCastRows(); document.getElementById('titleCastStatus').textContent='Voice selected. Save cast to apply.';
      }});
    });
    for (const [selector, key] of [['.cast-name', 'label'], ['.cast-category', 'category'], ['.cast-delivery', 'delivery']]) {
      box.querySelectorAll(selector).forEach(input => input.addEventListener('input', () => {
        castState.cast[Number(input.dataset.idx)][key] = input.value;
      }));
    }
  }

  async function openCast(item) {
    castState.item = item;
    const status = document.getElementById("titleCastStatus");
    const box = document.getElementById("titleCastRows");
    if (status) status.textContent = "";
    try {
      const fetches = [api("api/cast?" + castParams(item))];
      if (!castState.voices) fetches.push(api("api/voices"));
      const [cr, vr] = await Promise.all(fetches);
      if (!box?.isConnected || castState.item !== item) return;
      castState.cast = cr.cast || [];
      if (vr) castState.voices = vr.voices || [];
    } catch (e) {
      if (!box?.isConnected || castState.item !== item) return;
      castState.cast = [];
      if (status) status.textContent = "Cast unavailable — is the API reachable?";
    }
    renderCastRows();
    document.getElementById("titleCastSave").disabled = !castState.cast.length;
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
    if (item.episode_id && !item.path) body.key = `episode:${item.tvdb_id}:${item.episode_id}`;
    else if (item.path) body.path = item.path;
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
