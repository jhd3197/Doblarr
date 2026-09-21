import { api } from './api.js';
import { escapeHtml as esc } from './dom.js';
import { loadLanguages, languageName } from './languages.js';
import { baseOf, listen, openCorrection } from './knowledge-correction.js';

export function entriesQuery({ locale = '', kind = '', scope = '', status = '', q = '', page = 1, pageSize = 25 }) {
  const params = new URLSearchParams();
  if (locale) params.set('locale', locale);
  if (kind) params.set('kind', kind);
  if (scope) params.set('scope', scope);
  if (status) params.set('status', status);
  if (q) params.set('q', q);
  params.set('page', String(page));
  params.set('page_size', String(pageSize));
  return `knowledge/entries?${params}`;
}

export function pageCount(total, pageSize) {
  return Math.max(1, Math.ceil(total / pageSize));
}

const STATUS_LABEL = { proposed: 'Unreviewed', reviewed: 'Reviewed', 'needs-retest': 'Needs retest', retired: 'Retired' };
const TABS = [
  ['pronunciation', 'Pronunciations'],
  ['term', 'Terminology & phrases'],
  ['memory', 'Translation memory'],
  ['packs', 'Installed packs'],
];

export function createKnowledge() {
  const view = { tab: 'pronunciation', locale: '', scope: '', status: '', q: '', page: 1, pageSize: 25 };
  let voices = null;

  async function renderKnowledge() {
    const root = document.getElementById('knowledgeRoot');
    if (!root) return;
    root.innerHTML = '<p class="hint">Loading language knowledge…</p>';
    await loadLanguages();
    let coverage = { locales: [] };
    try { coverage = await api('knowledge/coverage'); } catch (e) { /* counts stay empty */ }
    root.innerHTML = `
      <div style="display:flex;align-items:baseline;gap:14px;flex-wrap:wrap;">
        <h3 style="font-size:18px;margin:0;">Language knowledge</h3>
        <span class="hint">Corrections you save apply to new dubs; existing jobs keep their frozen rules.</span>
      </div>
      <div class="knowledge-cards" style="display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:14px;margin-top:16px;">
        ${coverage.locales.map(c => `<div class="panel" style="padding:16px 18px;">
          <p style="margin:0;font-weight:700;">${esc(c.name)}</p>
          <p class="m" style="margin:4px 0 0;font-size:12.5px;">${esc(c.locale)}</p>
          <p class="hint" style="margin:8px 0 0;">${c.reviewed} reviewed · ${c.proposed} proposed${c['needs-retest'] ? ` · ${c['needs-retest']} needs retest` : ''}</p>
          <div class="episode-actions" style="margin-top:10px;">
            <button class="btn btn-secondary knowledge-add" data-locale="${esc(c.locale)}">Add a correction</button>
          </div></div>`).join('')}
      </div>
      <div style="display:flex;gap:6px;flex-wrap:wrap;margin:22px 0 12px;">
        ${TABS.map(([k, t]) => `<button type="button" class="tab knowledge-tab" data-tab="${k}" aria-current="${view.tab === k ? 'page' : 'false'}">${t}</button>`).join('')}
      </div>
      <div class="knowledge-body"></div>`;
    root.querySelectorAll('.knowledge-add').forEach(b => {
      b.onclick = () => openCorrection({ kind: 'pronunciation', locale: b.dataset.locale, onSaved: renderKnowledge });
    });
    root.querySelectorAll('.knowledge-tab').forEach(b => {
      b.onclick = () => { view.tab = b.dataset.tab; view.page = 1; renderKnowledge(); };
    });
    const body = root.querySelector('.knowledge-body');
    if (view.tab === 'memory') {
      try {
        const data = await api(`memory?page=${view.page}`);
        body.innerHTML = `<div class="panel" style="padding:20px 24px;">
          <p class="hint">Private complete lines saved from dialogue review. Enable reuse in Translation settings. Unknown context and unreviewed lines cannot bypass translation.</p>
          ${data.entries.map(e => `<p><strong>${esc(e.source_text)}</strong> → ${esc(e.target_text)} <span class="hint">${esc(e.source_lang)} → ${esc(e.target_locale)} · ${esc(e.status)} · revision ${e.revision}</span>
            <button class="btn btn-ghost memory-retire" data-id="${esc(e.id)}" ${e.status === 'retired' ? 'disabled' : ''}>Retire</button></p>`).join('') || '<p>No translations saved yet.</p>'}
          <button class="btn btn-ghost memory-prev" ${view.page <= 1 ? 'disabled' : ''}>Previous</button>
          <span>${view.page} / ${pageCount(data.total, 25)}</span>
          <button class="btn btn-ghost memory-next" ${view.page * 25 >= data.total ? 'disabled' : ''}>Next</button></div>`;
        body.querySelector('.memory-prev').onclick = () => { view.page--; renderKnowledge(); };
        body.querySelector('.memory-next').onclick = () => { view.page++; renderKnowledge(); };
        body.querySelectorAll('.memory-retire').forEach(b => { b.onclick = async () => {
          try { await api(`memory/${encodeURIComponent(b.dataset.id)}/retire`, { method: 'POST' }); renderKnowledge(); }
          catch (e) { b.textContent = e.message; }
        }; });
      } catch (e) { body.textContent = e.message; }
      return;
    }
    if (view.tab === 'packs') {
      await renderPacks(body);
      return;
    }
    body.innerHTML = `
      <div class="panel" style="padding:16px 20px;margin-bottom:14px;"><div style="display:flex;gap:12px;flex-wrap:wrap;align-items:end;">
        <label class="review-field">Search<input class="input knowledge-q" type="search" value="${esc(view.q)}" placeholder="Phrase, source wording, usage"></label>
        <label class="review-field">Locale<select class="input knowledge-locale"><option value="">All locales</option></select></label>
        <label class="review-field">Scope<select class="input knowledge-scope">${['', 'line', 'episode', 'movie', 'show', 'personal', 'pack'].map(s => `<option value="${s}" ${view.scope === s ? 'selected' : ''}>${s || 'All scopes'}</option>`).join('')}</select></label>
        <label class="review-field">Status<select class="input knowledge-status">${['', 'proposed', 'reviewed', 'needs-retest', 'retired'].map(s => `<option value="${s}" ${view.status === s ? 'selected' : ''}>${s ? STATUS_LABEL[s] : 'All statuses'}</option>`).join('')}</select></label>
        <button class="btn btn-secondary knowledge-search">Search</button>
      </div></div>
      <div class="knowledge-list"></div>
      <div class="knowledge-detail"></div>`;
    const localeSelect = body.querySelector('.knowledge-locale');
    coverage.locales.forEach(c => {
      const option = document.createElement('option');
      option.value = c.locale;
      option.textContent = c.name;
      option.selected = view.locale === c.locale;
      localeSelect.append(option);
    });
    if (view.locale && ![...localeSelect.options].some(o => o.value === view.locale)) {
      const option = document.createElement('option');
      option.value = view.locale; option.textContent = view.locale; option.selected = true;
      localeSelect.append(option);
    }
    body.querySelector('.knowledge-search').onclick = () => {
      view.q = body.querySelector('.knowledge-q').value.trim();
      view.locale = localeSelect.value;
      view.scope = body.querySelector('.knowledge-scope').value;
      view.status = body.querySelector('.knowledge-status').value;
      view.page = 1;
      loadList();
    };
    await loadList();
  }

  async function renderPacks(body) {
    try {
      const data = await api('packs');
      body.innerHTML = `<div class="panel" style="padding:20px 24px;">
        <p class="hint">Pack updates affect new jobs. Existing jobs retain their pinned revisions. Proposed content stays inactive.</p>
        <label class="review-field">Local pack JSON path<input class="input pack-path"></label>
        <button class="btn btn-secondary pack-install">Install file</button>
        <label class="review-field">Pack ID from configured distribution<input class="input pack-id"></label>
        <button class="btn btn-secondary pack-download">Download and install</button>
        <p class="pack-status hint" role="status"></p>
        ${data.packs.map(p => `<h4>${esc(p.name)} · ${p.third_party ? 'Third-party' : 'Official'}</h4>
          ${p.releases.map(r => `<p class="hint">${esc(r.release)} · ${r.active ? 'Active' : 'Retained'} · ${esc(JSON.stringify(r.coverage))}</p>`).join('')}
          <button class="btn btn-ghost pack-rollback" data-id="${esc(p.pack_id)}" ${p.releases.length < 2 ? 'disabled' : ''}>Roll back</button>`).join('') || '<p>No packs installed.</p>'}
      </div>`;
      const perform = async (button, path, payload) => {
        button.disabled = true;
        try { await api(path, { method: 'POST', json: payload }); await renderKnowledge(); }
        catch (e) { body.querySelector('.pack-status').textContent = e.message; button.disabled = false; }
      };
      body.querySelector('.pack-install').onclick = e => perform(e.target, 'packs/install', { path: body.querySelector('.pack-path').value });
      body.querySelector('.pack-download').onclick = e => perform(e.target, 'packs/download', { pack_id: body.querySelector('.pack-id').value });
      body.querySelectorAll('.pack-rollback').forEach(b => { b.onclick = () => perform(b, `packs/${encodeURIComponent(b.dataset.id)}/rollback`, {}); });
    } catch (e) { body.textContent = e.message; }
  }

  async function loadList() {
    const root = document.getElementById('knowledgeRoot');
    const list = root?.querySelector('.knowledge-list');
    if (!list) return;
    list.innerHTML = '<p class="hint">Searching…</p>';
    let data;
    try {
      data = await api(entriesQuery({ ...view, kind: view.tab }));
    } catch (e) { list.innerHTML = `<p class="hint">${esc(e.message)}</p>`; return; }
    const pages = pageCount(data.total, view.pageSize);
    list.innerHTML = `<div class="panel" style="padding:6px 18px 10px;">
      <table class="table"><thead><tr><th>Phrase</th><th style="width:130px;">Locale</th>
        <th style="width:110px;">Scope</th><th style="width:120px;">Status</th><th style="width:80px;">Rules</th></tr></thead>
      <tbody>${data.entries.map(e => `<tr class="knowledge-row" data-id="${esc(e.id)}" style="cursor:pointer;">
        <td style="font-weight:600;">${esc(e.phrase || '(suppression)')}${e.sense ? ` <span class="hint">· ${esc(e.sense)}</span>` : ''}</td>
        <td class="m" style="font-size:13px;">${esc(languageName(e.locale))}</td>
        <td class="m" style="font-size:13px;">${esc(e.scope)}</td>
        <td>${e.status === 'proposed' ? '<span class="tag tag-accent">Unreviewed</span>' : `<span class="tag tag-neutral">${STATUS_LABEL[e.status] || esc(e.status)}</span>`}</td>
        <td class="m" style="font-size:13px;">v${e.revision}</td></tr>`).join('')
      || '<tr><td colspan="5" class="hint">No entries match. Add a correction above.</td></tr>'}</tbody></table></div>
      <div class="episode-actions" style="margin-top:10px;">
        <button class="btn btn-ghost knowledge-prev" ${view.page <= 1 ? 'disabled' : ''}>← Newer</button>
        <span class="hint">Page ${view.page} of ${pages} · ${data.total} entries</span>
        <button class="btn btn-ghost knowledge-next" ${view.page >= pages ? 'disabled' : ''}>Older →</button>
      </div>`;
    list.querySelector('.knowledge-prev').onclick = () => { view.page -= 1; loadList(); };
    list.querySelector('.knowledge-next').onclick = () => { view.page += 1; loadList(); };
    list.querySelectorAll('.knowledge-row').forEach(row => { row.onclick = () => showDetail(row.dataset.id); });
  }

  async function showDetail(id) {
    const root = document.getElementById('knowledgeRoot');
    const box = root?.querySelector('.knowledge-detail');
    if (!box) return;
    box.innerHTML = '<p class="hint">Loading entry…</p>';
    let data;
    try { data = await api(`knowledge/entries/${id}`); } catch (e) { box.innerHTML = `<p class="hint">${esc(e.message)}</p>`; return; }
    const e = data.entry;
    if (voices === null) {
      try { voices = (await api('voice-catalog')).voices || []; } catch { voices = []; }
    }
    const voiceChoices = voices.filter(v => !e.locale || baseOf(e.locale) === String(v.language || '').toLowerCase());
    box.innerHTML = `<div class="panel" style="padding:20px 24px;margin-top:14px;">
      <div style="display:flex;align-items:baseline;gap:12px;flex-wrap:wrap;">
        <h3 style="margin:0;font-size:17px;">${esc(e.phrase || '(suppression)')}</h3>
        <span class="m hint">${esc(e.kind)} · ${esc(languageName(e.locale))} · ${esc(e.scope)}${e.scope_ref ? ` · ${esc(e.scope_ref)}` : ''} · revision ${e.revision} · ${STATUS_LABEL[e.status] || esc(e.status)}${e.origin === 'installed' ? ' · installed pack' : ''}</span>
        <span style="margin-left:auto;" class="episode-actions">
          <button class="btn btn-ghost knowledge-retire" ${e.status === 'retired' ? 'disabled' : ''}>Retire</button>
        </span></div>
      ${e.pronunciation ? `<p style="margin:10px 0 0;">Intended sound: ${esc(e.pronunciation)}${e.ipa ? ` <span class="m">/${esc(e.ipa)}/</span>` : ''}</p>` : ''}
      ${e.usage ? `<p class="hint" style="margin:6px 0 0;">Usage: ${esc(e.usage)}</p>` : ''}
      ${e.examples.length ? `<p class="hint" style="margin:6px 0 0;">Examples: ${e.examples.map(x => esc(x)).join(' · ')}</p>` : ''}
      ${e.suppresses ? `<p class="hint" style="margin:6px 0 0;">Suppresses rule ${esc(e.suppresses)}</p>` : ''}
      ${data.suppressed_by.length ? `<p class="hint" style="margin:6px 0 0;">Suppressed by: ${data.suppressed_by.map(s => esc(s.phrase || s.id)).join(', ')}</p>` : ''}
      <h4 style="margin:16px 0 6px;font-size:14px;">Engine realizations (${data.realizations.length})</h4>
      ${data.realizations.length ? `<table class="table"><thead><tr><th>Replacement</th><th>Engine</th><th>Model</th><th>Voice</th><th>Status</th></tr></thead>
        <tbody>${data.realizations.map(r => `<tr><td style="font-weight:600;">${esc(r.replacement)}</td>
          <td class="m">${esc(r.engine)}</td><td class="m">${esc(r.model ?? 'any (broad)')}</td>
          <td class="m">${esc(r.voice ?? 'any')}</td><td>${r.status === 'proposed' ? '<span class="tag tag-accent">Unreviewed</span>' : `<span class="tag tag-neutral">${STATUS_LABEL[r.status] || esc(r.status)}</span>`}</td></tr>`).join('')}</tbody></table>`
      : '<p class="hint">No tested realization yet — this rule stays silent for every engine until one is added.</p>'}
      <div style="display:flex;gap:12px;flex-wrap:wrap;align-items:end;margin-top:14px;">
        <label class="review-field">Audition sentence<input class="input knowledge-sample" value="${esc(e.phrase)}"></label>
        <label class="review-field">Voice<select class="input knowledge-voice">
          ${voiceChoices.map(v => `<option value="${esc(v.key)}">${esc(v.name)} (${esc(v.engine)})</option>`).join('')}</select></label>
        <button class="btn btn-secondary knowledge-listen" ${voiceChoices.length ? '' : 'disabled'}>Listen</button>
      </div>
      <audio controls class="knowledge-audio" hidden></audio>
      <p class="knowledge-status hint" role="status"></p>
      <details style="margin-top:10px;"><summary>Review history (${e.review_history.length})</summary>
        <pre style="white-space:pre-wrap;overflow-wrap:anywhere">${esc(e.review_history.length ? JSON.stringify(e.review_history, null, 2) : 'No recorded review yet.')}</pre></details>
    </div>`;
    const status = box.querySelector('.knowledge-status');
    box.querySelector('.knowledge-listen').onclick = event => {
      const button = event.currentTarget;
      button.disabled = true;
      listen({
        key: box.querySelector('.knowledge-voice').value,
        text: box.querySelector('.knowledge-sample').value,
        language: baseOf(e.locale) || 'en',
        audio: box.querySelector('.knowledge-audio'), status,
        alive: () => box.isConnected, onDone: () => { button.disabled = false; },
      });
    };
    box.querySelector('.knowledge-retire').onclick = async () => {
      try {
        await api(`knowledge/entries/${id}/retire`, { method: 'POST' });
        status.textContent = 'Retired. Future dubs will not use it; frozen jobs are unchanged.';
        loadList();
      } catch (error) { status.textContent = error.message; }
    };
  }

  return { renderKnowledge };
}
