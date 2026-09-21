import { api } from './api.js';
import { escapeHtml as esc } from './dom.js';

const labels = { 'audio-present': 'Audio present', 'dub-ready': 'AI dub ready',
  'not-downloaded': 'Not downloaded', 'needs-dub': 'Needs dub', 'unknown-audio': 'Audio unknown',
  queued: 'Queued', running: 'Generating', failed: 'Failed — retry available' };

export async function renderEpisodes(root, { item, target, targets, onTarget, onCast, openWatch }) {
  root.innerHTML = '<p class="hint">Loading seasons and episodes from Sonarr…</p>';
  let data, filter = 'all', selected = new Set();
  const current = () => root.isConnected;
  async function load(refresh = false) {
    data = await api(`series/${item.tvdb_id}/episodes?target_lang=${encodeURIComponent(target)}&refresh=${refresh}`);
  }
  async function queue(ids, kind = 'full', missingOnly = true) {
    const status = root.querySelector('.episode-feedback');
    root.querySelectorAll('button').forEach(b => { b.disabled = true; });
    status.textContent = 'Queuing episode files…';
    try {
      const result = await api(`series/${item.tvdb_id}/queue`, { method: 'POST', json: {
        episode_ids: ids, target_lang: target, kind, missing_only: missingOnly,
      } });
      if (!current()) return;
      const message = `${result.queued.length} file${result.queued.length === 1 ? '' : 's'} queued.`
        + (result.skipped.length ? ` Skipped: ${[...new Set(result.skipped.map(s => s.reason))].join('; ')}.` : '');
      await load(); selected.clear(); render();
      root.querySelector('.episode-feedback').textContent = message;
    } catch (e) {
      if (current()) { render(); root.querySelector('.episode-feedback').textContent = e.message; }
    }
  }
  function render() {
    if (!current()) return;
    const rows = data.episodes.filter(e => filter === 'all' || (filter === 'downloaded' ? e.downloaded : e.downloaded && !e.dubbed));
    const seasons = [...new Set(rows.map(e => e.season))];
    root.innerHTML = `<div class="episode-toolbar"><div><h3>Episodes</h3>
      <p class="hint">${data.downloaded} downloaded of ${data.total} episodes · ${data.dubbed} with ${esc(target.toUpperCase())} audio or a completed AI dub</p></div>
      <label>Dub language<select class="input episode-target" aria-label="Dub language">${[...new Set([...targets, target])].map(t => `<option value="${esc(t)}" ${t === target ? 'selected' : ''}>${esc(t.toUpperCase())}</option>`).join('')}</select></label>
      <label>Show<select class="input episode-filter" aria-label="Show episodes"><option value="all">All episodes</option><option value="downloaded">Downloaded</option><option value="missing">Missing dub</option></select></label></div>
      <div class="episode-actions"><button class="btn btn-primary episode-selected" ${selected.size ? '' : 'disabled'}>Queue selected (${selected.size})</button>
      <button class="btn btn-secondary episode-missing" ${data.episodes.some(e => e.downloaded && !e.dubbed && !e.job_id) ? '' : 'disabled'}>Queue missing dubs</button>
      <button class="btn btn-ghost episode-refresh">Refresh episodes</button></div>
      <p class="hint">Audio present means Sonarr reports that language in the source file. AI dub ready is a separate generated output. Teases and auditions do not count as full dubs.</p>
      <p class="episode-feedback" role="status"></p>
      ${seasons.length ? seasons.map(season => `<details class="episode-season" ${season === (rows.find(e => e.downloaded)?.season ?? seasons[0]) ? 'open' : ''}><summary>${season === 0 ? 'Specials' : `Season ${season}`} <span class="hint">${rows.filter(e => e.season === season).length} episodes</span></summary>
      ${rows.filter(e => e.season === season).map(e => `<div class="episode-row" data-episode="${e.id}">
        <input class="episode-check" type="checkbox" aria-label="Select S${String(e.season).padStart(2, '0')}E${String(e.episode).padStart(2, '0')}" ${selected.has(e.id) ? 'checked' : ''} ${!e.downloaded || e.job_id ? 'disabled' : ''}>
        <div class="episode-name"><span class="m">S${String(e.season).padStart(2, '0')}E${String(e.episode).padStart(2, '0')}</span><a href="/title/tvdb-${item.tvdb_id}/episode/${e.id}/voices" class="episode-open"><strong>${esc(e.title)}</strong></a><span class="hint">${e.audio_langs.length ? e.audio_langs.map(l => esc(l.toUpperCase())).join(' · ') : 'No audio metadata'}</span></div>
        <span class="tag ${e.dubbed ? 'tag-neutral' : 'tag-outline'}">${esc(labels[e.status] || e.status)}</span>
        <div class="episode-row-actions">${e.downloaded ? `<button class="btn btn-ghost episode-cast">Voices</button><button class="btn btn-secondary episode-audition" ${e.job_id ? 'disabled' : ''}>Audition</button><button class="btn btn-primary episode-queue" ${e.job_id ? 'disabled' : ''}>${e.dubbed ? 'Create AI dub' : 'Queue dub'}</button>` : ''}
        ${e.output_job_id ? '<button class="btn btn-ghost episode-watch">Watch dub</button>' : ''}</div></div>`).join('')}</details>`).join('') : '<p class="hint">No episodes match this filter.</p>'}`;
    root.querySelector('.episode-filter').value = filter;
    root.querySelector('.episode-filter').onchange = e => { filter = e.target.value; render(); };
    root.querySelector('.episode-target').onchange = e => onTarget(e.target.value);
    root.querySelector('.episode-selected').onclick = () => queue([...selected], 'full', false);
    root.querySelector('.episode-missing').onclick = () => queue(data.episodes.filter(e => e.downloaded && !e.dubbed && !e.job_id).map(e => e.id));
    root.querySelector('.episode-refresh').onclick = async () => {
      try { await load(true); render(); } catch(e) { if (current()) root.querySelector('.episode-feedback').textContent = e.message; }
    };
    root.querySelectorAll('[data-episode]').forEach(row => {
      const episode = data.episodes.find(e => e.id === Number(row.dataset.episode));
      row.querySelector('.episode-check').onchange = e => {
        if (e.target.checked) selected.add(episode.id); else selected.delete(episode.id);
        const button = root.querySelector('.episode-selected');
        button.textContent = `Queue selected (${selected.size})`; button.disabled = !selected.size;
      };
      row.querySelector('.episode-open').onclick = event => { if (!event.ctrlKey && !event.metaKey) { event.preventDefault(); onCast(episode); } };
      row.querySelector('.episode-queue')?.addEventListener('click', () => queue([episode.id], 'full', false));
      row.querySelector('.episode-audition')?.addEventListener('click', () => queue([episode.id], 'audition', false));
      row.querySelector('.episode-cast')?.addEventListener('click', () => onCast(episode));
      row.querySelector('.episode-watch')?.addEventListener('click', () => openWatch(episode.output_job_id));
    });
  }
  try { await load(); render(); }
  catch(e) { if (current()) root.textContent = `Episodes unavailable: ${e.message}`; }
}
