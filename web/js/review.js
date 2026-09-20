import { api, apiUrl } from './api.js';
import { escapeHtml as esc, safeGet } from './dom.js';

export function createReview({ onQueued }) {
  const $ = id => document.getElementById(id);
  const dialog = $('reviewDialog'), list = $('reviewLines'), editor = $('reviewEditor');
  const status = $('reviewStatus'), submit = $('reviewSubmit');
  let data, jobId, selected, voices = [], pending = new Map(), opener, epoch = 0;
  const clock = s => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, '0')}`;

  function renderList() {
    if (!data) return;
    const rows = data.segments.filter(s => !$('reviewFlagged').checked || s.issues.length);
    list.innerHTML = rows.map(s => `<button type="button" class="review-line" data-line="${s.index}" aria-pressed="${s.index === selected}">
      <span class="m">${clock(s.source_start ?? s.start)}</span><span><strong>${esc(s.speaker)}</strong>
      <span class="review-excerpt">${esc(s.text_translated || s.text_src)}</span></span>
      <span class="review-marker">${pending.has(s.index) ? 'Edited' : s.issues.length ? 'Review' : ''}</span></button>`).join('')
      || '<p class="hint">No lines match this filter.</p>';
    submit.textContent = pending.size ? `Render ${pending.size} changed line${pending.size === 1 ? '' : 's'}` : 'Render changes';
    submit.disabled = !pending.size || !data.editable;
  }

  function select(index) {
    editor.querySelector('audio')?.pause();
    selected = index;
    const row = data.segments.find(s => s.index === index);
    if (!row) { editor.innerHTML = '<p>Select a line to review.</p>'; renderList(); return; }
    const edit = pending.get(index) || {};
    const key = safeGet('doblarr_api_key', '');
    const audio = apiUrl(`jobs/${jobId}/clips/${index}`) + (key ? `?api_key=${encodeURIComponent(key)}` : '');
    const voice = edit.voice ?? row.voice ?? '';
    const choices = [...voices];
    if (voice && !choices.some(v => v.id === voice)) choices.push({ id: voice, name: voice });
    editor.innerHTML = `<div class="review-line-heading"><h3>Line ${index + 1}</h3><span class="m">${esc(row.speaker)}</span></div>
      <p class="review-source">${esc(row.text_src)}</p>
      ${row.has_audio ? `<audio controls preload="none" src="${esc(audio)}" aria-label="Generated line ${index + 1}"></audio>` : '<p class="hint">No generated audio yet.</p>'}
      ${row.issues.length ? `<p class="review-flags">${row.issues.map(i => esc(i.replaceAll('_', ' '))).join(' · ')}</p>` : ''}
      <label class="review-field">Dubbed dialogue<textarea id="reviewText" class="input" rows="4">${esc(edit.text ?? row.text_translated ?? row.text_src)}</textarea></label>
      <div class="review-timing"><label class="review-field">Start (seconds)<input id="reviewStart" class="input m" type="number" min="0" step="0.01" value="${edit.start ?? row.start}"></label>
      <label class="review-field">End (seconds)<input id="reviewEnd" class="input m" type="number" min="0" step="0.01" value="${edit.end ?? row.end}"></label></div>
      <label class="review-field">Voice<select id="reviewVoice" class="input"><option value="">Use character voice</option>${choices.map(v => `<option value="${esc(v.id)}" ${voice === v.id ? 'selected' : ''}>${esc(v.name)}</option>`).join('')}</select></label>
      <label class="review-field">Delivery<input id="reviewDelivery" class="input" maxlength="500" value="${esc(edit.delivery ?? row.delivery ?? '')}" placeholder="For example: speak quietly"></label>
      <p class="hint">Delivery instructions require a Qwen voice engine.</p>
      <div class="review-options"><label><input id="reviewRegenerate" type="checkbox" ${edit.regenerate ? 'checked' : ''}> Generate a new take</label>
      <label><input id="reviewExclude" type="checkbox" ${edit.exclude ? 'checked' : ''}> Exclude this line</label></div>`;
    editor.querySelectorAll('input,textarea,select').forEach(f => { f.disabled = !data.editable; });
    renderList();
  }

  function collect() {
    const row = data.segments.find(s => s.index === selected);
    if (!row) return;
    const patch = { index: selected, text: $('reviewText').value,
      start: Number($('reviewStart').value), end: Number($('reviewEnd').value),
      voice: $('reviewVoice').value, delivery: $('reviewDelivery').value,
      regenerate: $('reviewRegenerate').checked, exclude: $('reviewExclude').checked };
    const changed = patch.text !== (row.text_translated || row.text_src) || patch.start !== row.start
      || patch.end !== row.end || patch.voice !== (row.voice || '') || patch.delivery !== (row.delivery || '')
      || patch.regenerate || patch.exclude;
    if (changed) pending.set(selected, patch); else pending.delete(selected);
    status.textContent = '';
    renderList();
  }
  editor.addEventListener('input', collect);
  list.addEventListener('click', e => {
    const button = e.target.closest('[data-line]');
    if (button) select(Number(button.dataset.line));
  });
  $('reviewFlagged').addEventListener('change', renderList);
  $('reviewClose').addEventListener('click', () => dialog.close());
  dialog.addEventListener('close', () => {
    editor.querySelector('audio')?.pause();
    const target = opener?.isConnected ? opener
      : [...document.querySelectorAll('.job-review')].find(button => button.dataset.id === jobId);
    target?.focus();
  });
  submit.addEventListener('click', async () => {
    if (!pending.size) return;
    const edits = [...pending.values()];
    if (edits.some(e => !Number.isFinite(e.start) || !Number.isFinite(e.end) || e.start < 0
      || e.end <= e.start || !e.text.trim())) {
      status.textContent = 'Each line needs dialogue and an end time after its start.'; return;
    }
    submit.disabled = true;
    status.textContent = 'Queuing changes…';
    try {
      await api(`jobs/${jobId}/review`, { method: 'POST', json: { edits } });
      pending.clear(); data.editable = false; select(selected);
      status.textContent = 'Queued. Unchanged clips will be reused. Close to follow progress.';
      onQueued();
    } catch (error) { status.textContent = error.message; submit.disabled = false; }
  });

  async function open(id) {
    const version = ++epoch;
    data = null;
    opener = document.activeElement; jobId = id; pending = new Map();
    list.innerHTML = ''; editor.innerHTML = '<p>Loading dialogue…</p>';
    status.textContent = ''; submit.disabled = true;
    $('reviewTitle').textContent = 'Review dialogue'; $('reviewSummary').textContent = '';
    dialog.showModal();
    try {
      const result = await api(`jobs/${id}/review`);
      if (version !== epoch) return;
      data = result;
      $('reviewTitle').textContent = data.title;
      $('reviewSummary').textContent = `${data.segments.length} lines · ${data.flagged} flagged for review`;
      $('reviewFlagged').checked = data.flagged > 0;
      voices = [];
      select((data.segments.find(s => s.issues.length) || data.segments[0])?.index);
      // Voice discovery must not block listening or discard edits made while it loads.
      api('voices').then(result => {
        if (version !== epoch) return;
        voices = result.voices || [];
        const field = $('reviewVoice');
        if (!field) return;
        const known = new Set([...field.options].map(option => option.value));
        voices.forEach(voice => {
          if (known.has(voice.id)) return;
          const option = document.createElement('option');
          option.value = voice.id; option.textContent = voice.name; field.append(option);
        });
      }).catch(() => {});
    } catch (error) { editor.textContent = error.message; }
  }
  return { open };
}
