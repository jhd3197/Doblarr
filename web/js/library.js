import { library, matchesSearch } from './state.js';
import { escapeHtml } from './dom.js';
import { castParams } from './identity.js';
import { api } from './api.js';
import { getJobs } from './jobs-data.js';

export function createLibrary({ goTitle }) {
  function statusTag(label) {
    const cls = label === "needs-dub" ? "tag-accent"
              : (label === "partial" || label === "review") ? "tag-outline" : "tag-neutral";
    return `<span class="tag ${cls}">${label}</span>`;
  }

  // ISO 639-1 -> flag emoji. Windows browsers don't render regional-indicator
  // emoji, so chips ALWAYS carry the code text — the emoji is a bonus glyph.
  const LANG_FLAGS = {
    en: "🇬🇧", es: "🇪🇸", ja: "🇯🇵", fr: "🇫🇷", de: "🇩🇪", it: "🇮🇹", pt: "🇵🇹",
    ko: "🇰🇷", zh: "🇨🇳", hi: "🇮🇳", ru: "🇷🇺", ar: "🇸🇦",
  };

  function langChip(code, missing) {
    const flag = LANG_FLAGS[code] ? LANG_FLAGS[code] + " " : "";
    const tip = missing ? `no ${code.toUpperCase()} audio (target language)` : `${code.toUpperCase()} audio present`;
    return `<span class="lang-chip${missing ? " missing" : ""}" title="${tip}">${flag}${escapeHtml(code.toUpperCase())}</span>`;
  }

  function langChipsHtml(i) {
    // Audio languages present; fall back to the original language when unprobed.
    const present = new Set((i.audio_langs && i.audio_langs.length)
      ? i.audio_langs : (i.original && i.original !== "??" ? [i.original] : []));
    const targets = library.targets || [];
    return [...present].map(c => langChip(c, false)).join("")
      + targets.filter(t => !present.has(t)).map(t => langChip(t, true)).join("");
  }

  function renderLibrary() {
    const grid = document.getElementById("libraryGrid");
    const rows = library.items.filter(i =>
      (library.filter === "all" || i.status === library.filter) && matchesSearch(i.title));
    if (library.sort === "recent")  // titles with job activity first, newest on top
      rows.sort((a, b) => (library.activity[b.title] || "").localeCompare(library.activity[a.title] || ""));
    if (!rows.length) {
      grid.innerHTML = `<div style="padding:22px 10px;color:var(--muted);">No titles match.</div>`;
      return;
    }
    library.rendered = rows;
    grid.innerHTML = rows.map((i, idx) => {
      const poster = i.poster
        ? `<img class="poster-img" loading="lazy" src="${escapeHtml(i.poster)}" alt=""
                onerror="this.onerror=null;this.hidden=true;this.nextElementSibling.hidden=false">
           <div class="poster-fallback" hidden>${escapeHtml(i.title)}</div>`
        : `<div class="poster-fallback">${escapeHtml(i.title)}</div>`;
      // Every card gets all three actions — an AI track can be added even when
      // the title already has the target language (e.g. "English AI").
      const action = `<div style="display:flex;gap:2px;flex-wrap:wrap;">
        <button type="button" class="btn btn-ghost queue-dub" data-idx="${idx}">Queue dub</button>
        <button type="button" class="btn btn-ghost tease-dub" data-idx="${idx}">Tease</button>
        <button type="button" class="btn btn-ghost cast-edit" data-idx="${idx}">Cast</button>
      </div>`;
      return `
      <div class="poster-card" data-idx="${idx}" style="cursor:pointer;">
        ${poster}
        <div class="poster-body">
          <div style="font-weight:600;font-size:13.5px;line-height:1.3;">${escapeHtml(i.title)}${i.year ? ` <span class="m" style="color:var(--muted);font-size:12px;">${i.year}</span>` : ""}</div>
          <div class="lang-chips">${langChipsHtml(i)}</div>
          <div style="display:flex;align-items:center;justify-content:space-between;gap:8px;margin-top:auto;">
            ${statusTag(i.label)}
            <span class="m" style="font-size:11.5px;color:var(--muted);">${escapeHtml(i.source.split(" ")[0])}</span>
          </div>
          ${action}
        </div>
      </div>`;
    }).join("");
  }

  async function fetchPlan(item) {
    const data = await api("plan?" + castParams(item));
    return data.plan || {};
  }

  async function queueDub(item, btn, kind = "full") {
    btn.disabled = true; btn.textContent = kind === "tease" ? "Teasing…" : "Queuing…";
    try {
      // The title's dub plan rides along: target_lang picks the job's language,
      // the rest are per-title config overrides the worker merges in.
      const plan = await fetchPlan(item);
      const overrides = { ...plan };
      const target = overrides.target_lang;
      delete overrides.target_lang;
      await api("jobs", {
        method: "POST",
        json: {
          title: item.title, source: item.source, source_lang: item.original,
          target_lang: target || (library.targets && library.targets[0]) || "en", path: item.path,
          kind,
          overrides: Object.keys(overrides).length ? overrides : undefined,
        },
      });
      btn.textContent = kind === "tease" ? "Teased ✓" : "Queued ✓";
    } catch (err) {
      btn.disabled = false; btn.textContent = "Retry";
      btn.title = err.message;
      window.alert("Could not queue dub: " + err.message);
    }
  }

  async function loadLibrary() {
    const grid = document.getElementById("libraryGrid");
    grid.innerHTML = `<div style="padding:22px 10px;color:var(--muted);">Scanning Radarr…</div>`;
    try {
      const data = await api("library");
      library.items = data.items || [];
      library.targets = data.target_languages || ["en"];
      library.loaded = true;
      renderLibrary();
      // Job activity per title, for the "Recently active" sort.
      try {
        const data = await getJobs();
        data.jobs?.forEach(j => {
          const t = j.updated_at || "";
          if (!library.activity[j.title] || library.activity[j.title] < t)
            library.activity[j.title] = t;
        });
        if (library.sort === "recent") renderLibrary();
      } catch (e) {}
    } catch (err) {
      library.loaded = false;
      grid.innerHTML = `<div style="padding:22px 10px;color:var(--muted);">Library scan unavailable — is Doblarr running (doblarr serve) and Radarr reachable? ${escapeHtml(err.message)}</div>`;
    }
  }

  return { statusTag, langChipsHtml, renderLibrary, fetchPlan, queueDub, loadLibrary };
}
