import { api } from './api.js';
import { escapeHtml as esc } from './dom.js';
import { openCorrection } from './knowledge-correction.js';
import { FILTERS, createSceneSection, matchesFilter } from './review-scene.js';

export function createReview({ onQueued }) {
  const $ = id => document.getElementById(id);
  const sceneHost = document.createElement('div');
  sceneHost.id = 'reviewScene';
  const dialog = $('reviewDialog'), list = $('reviewLines'), editor = $('reviewEditor');
  const status = $('reviewStatus'), submit = $('reviewSubmit');
  let data, jobId, selected, voices = [], pending = new Map(), opener, epoch = 0;
  const clock = s => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, '0')}`;
  const currentRow = () => data?.segments.find(s => s.index === selected);

  // The scene panel owns playback, takes, direction, level and verdicts. It
  // lives inside the editor so one line is the unit of work, and it is torn
  // down with the dialog so no audio or poll outlives the review.
  const scene = createSceneSection({
    container: sceneHost,
    getJobId: () => jobId,
    getRow: currentRow,
    getData: () => data,
    // A saved verdict must not re-render the editor underneath the reviewer:
    // the line list marker is the only thing that needs refreshing, and
    // rebuilding the panel would wipe the confirmation they just read.
    onDecision: () => renderList(),
  });

  function filter() {
    const chosen = $('reviewFilter');
    if (chosen && chosen.value) return chosen.value;
    return $('reviewFlagged').checked ? 'flagged' : 'all';
  }

  function renderList() {
    if (!data) return;
    const rows = data.segments.filter(s => matchesFilter(s, filter()));
    list.innerHTML = rows.map(s => `<button type="button" class="review-line" data-line="${s.index}" aria-pressed="${s.index === selected}">
      <span class="m">${clock(s.source_start ?? s.start)}</span><span><strong>${esc(s.speaker)}</strong>
      <span class="review-excerpt">${esc(s.text_translated || s.text_src)}</span></span>
      <span class="review-marker">${pending.has(s.index) ? 'Edited' : s.issues.length ? 'Review' : ''}</span></button>`).join('')
      || '<p class="hint">No lines match this filter.</p>';
    const events = scene.events().length;
    const parts = [];
    if (pending.size) {
      parts.push(`${pending.size} changed line${pending.size === 1 ? '' : 's'}`);
    }
    if (events) parts.push(`${events} reaction${events === 1 ? '' : 's'}`);
    submit.textContent = parts.length ? `Render ${parts.join(' and ')}` : 'Render changes';
    submit.disabled = (!pending.size && !events) || !data.editable;
  }

  function select(index) {
    scene.stop();
    editor.querySelector('audio')?.pause();
    selected = index;
    const row = data.segments.find(s => s.index === index);
    if (!row) { editor.innerHTML = '<p>Select a line to review.</p>'; renderList(); return; }
    const edit = pending.get(index) || {};
    const voice = edit.voice ?? row.voice ?? '';
    const choices = [...voices];
    if (voice && !choices.some(v => v.id === voice)) choices.push({ id: voice, name: voice });
    editor.innerHTML = `<div class="review-line-heading"><h3>Line ${index + 1}</h3><span class="m">${esc(row.speaker)}</span></div>
      <p class="review-source">${esc(row.text_src)}</p>
      ${row.has_audio ? '' : '<p class="hint">No generated audio yet.</p>'}
      ${row.issues.length ? `<p class="review-flags">${row.issues.map(i => esc(i.replaceAll('_', ' '))).join(' · ')}</p>` : ''}
      <label class="review-field">Dubbed dialogue<textarea id="reviewText" class="input" rows="4">${esc(edit.text ?? row.text_translated ?? row.text_src)}</textarea></label>
      <div class="review-timing"><label class="review-field">Start (seconds)<input id="reviewStart" class="input m" type="number" min="0" step="0.01" value="${edit.start ?? row.start}"></label>
      <label class="review-field">End (seconds)<input id="reviewEnd" class="input m" type="number" min="0" step="0.01" value="${edit.end ?? row.end}"></label></div>
      <label class="review-field">Voice<select id="reviewVoice" class="input"><option value="">Use character voice</option>${choices.map(v => `<option value="${esc(v.id)}" ${voice === v.id ? 'selected' : ''}>${esc(v.name)}</option>`).join('')}</select></label>
      <label class="review-field">Delivery<input id="reviewDelivery" class="input" maxlength="500" value="${esc(edit.delivery ?? row.delivery ?? '')}" placeholder="For example: speak quietly"></label>
      <p class="hint">Delivery instructions require a Qwen voice engine.</p>
      <div class="review-options"><label><input id="reviewRegenerate" type="checkbox" ${edit.regenerate ? 'checked' : ''}> Generate a new take</label>
      <label><input id="reviewExclude" type="checkbox" ${edit.exclude ? 'checked' : ''}> Exclude this line</label></div>
      <div class="review-options"><button type="button" class="btn btn-ghost" id="reviewFixPron">Fix pronunciation</button>
      <button type="button" class="btn btn-ghost" id="reviewFixTerm">Improve regional wording</button>
      <button type="button" class="btn btn-ghost" id="reviewSaveMemory">Save translation for reuse</button></div>
      <p class="hint">Translation: ${esc(row.translation_provenance?.method || 'legacy')} · ${esc(row.translation_provenance?.reason || 'No recorded provenance')}</p>
      <p class="hint">${provenance(row)}</p>
      <p class="hint">${preparation(row)}</p>
      <div id="reviewScene"></div>
      <div id="reviewCorrection" class="review-correction"></div>`;
    editor.querySelectorAll('input,textarea,select').forEach(f => { f.disabled = !data.editable; });
    // The scene panel is appended rather than inlined so its audio element
    // survives a re-render of the surrounding editor.
    const host = editor.querySelector('#reviewScene');
    host.replaceWith(sceneHost);
    scene.load(row);
    wireCorrection(row);
    renderList();
  }

  // What this line actually is and what it was rendered from. Unknown stays
  // visible as unknown; it is not the same as "nothing to report".
  function provenance(row) {
    const cue = row.cue || {};
    const source = (cue.source?.spans || [])
      .map(s => `${clock(s.start)}–${clock(s.end)}`).join(', ') || 'unrecorded';
    const takes = cue.audio?.takes?.length || 0;
    const rendered = (cue.audio?.renders || []).slice(-1)[0];
    const role = rendered ? rendered.role : (cue.audio?.takes?.length ? 'raw' : 'none');
    const proven = rendered && rendered.proven === false ? ' (unverified role)' : '';
    return `Cue ${esc(cue.cue_id || 'unassigned')} · source ${esc(source)} · `
      + `${takes} take${takes === 1 ? '' : 's'} · rendered from ${esc(role)}${proven}`;
  }

  // What boundary preparation decided, and why. "kept" and "uncertain" are not
  // failures — leaving a take alone is the safe answer — so both say so plainly.
  function preparation(row) {
    const p = row.cue?.preparation;
    if (!p || !p.decision || p.decision === 'unknown') return 'Boundaries: not analyzed.';
    const parts = [];
    if (p.decision === 'trimmed') {
      parts.push(`trimmed ${(p.lead ?? 0).toFixed(2)}s lead / ${(p.tail ?? 0).toFixed(2)}s tail`);
    } else {
      parts.push(`${esc(p.decision)}${p.reason ? ` — ${esc(p.reason)}` : ''}`);
    }
    if (p.active_duration) parts.push(`${p.active_duration.toFixed(2)}s of speech`);
    if (p.onset) parts.push(`speaks ${p.onset.toFixed(2)}s after the cue starts`);
    if (p.silences?.length) parts.push(`${p.silences.length} internal pause${p.silences.length === 1 ? '' : 's'} kept`);
    const edged = (row.cue?.audio?.renders || []).some(r => r.role === 'edged');
    if (edged) parts.push('edges faded');
    return `Boundaries: ${parts.join(' · ')}`;
  }

  function wireCorrection(row) {
    const box = editor.querySelector('#reviewCorrection');
    const lineRef = data.title_ref ? `${data.title_ref}#${row.index}` : '';
    const lineText = row.text_translated || row.text_src;
    function open(kind) {
      openCorrection({
        kind, locale: data.locale || data.language || 'es',
        text: lineText, voice: row.profile || row.voice || '',
        titleRef: data.title_ref || '', showRef: data.show_ref || '', lineRef,
        onSaved: entry => {
          const affected = data.segments
            .filter(s => entry.phrase && (s.text_translated || s.text_src).includes(entry.phrase))
            .map(s => s.index);
          box.innerHTML = `<p class="hint">Correction saved for ${esc(entry.locale)} (${esc(entry.scope)} scope).
            ${affected.length} line${affected.length === 1 ? '' : 's'} use “${esc(entry.phrase)}” here.</p>
            <button type="button" class="btn btn-secondary" id="reviewRerender" ${affected.length && data.editable ? '' : 'disabled'}>Re-render affected lines with the correction</button>`;
          box.querySelector('#reviewRerender').onclick = async event => {
            const button = event.currentTarget;
            button.disabled = true;
            try {
              await api(`jobs/${jobId}/review`, { method: 'POST', json: {
                edits: affected.map(index => ({ index, regenerate: true })),
                use_updated_knowledge: true, base_revision: data.revision,
              } });
              data.editable = false;
              box.innerHTML = '<p class="hint">Queued with the updated knowledge. Unchanged clips will be reused.</p>';
              onQueued();
            } catch (error) { box.querySelector('.hint').textContent = error.message; button.disabled = false; }
          };
        },
      });
    }
    editor.querySelector('#reviewFixPron').onclick = () => open('pronunciation');
    editor.querySelector('#reviewFixTerm').onclick = () => open('term');
    editor.querySelector('#reviewSaveMemory').onclick = () => {
      box.innerHTML = `<p class="hint">Private complete-line memory. Matching scene, speaker register, settings and timing are required. Record only reviews you have performed.</p>
        <label class="review-field">Reviewer<input class="input memory-reviewer"></label>
        <label><input type="checkbox" class="memory-meaning"> Source meaning verified</label>
        <label><input type="checkbox" class="memory-natural"> Regional wording verified</label>
        <label><input type="checkbox" class="memory-timing"> Timing verified by listening</label>
        <button class="btn btn-secondary memory-save">Save privately</button><p class="memory-status hint" role="status"></p>`;
      box.querySelector('.memory-save').onclick = async event => {
        const button = event.currentTarget;
        const meaning = box.querySelector('.memory-meaning').checked;
        const natural = box.querySelector('.memory-natural').checked;
        const timing = box.querySelector('.memory-timing').checked;
        button.disabled = true;
        try {
          await api('memory', { method: 'POST', json: {
            source_lang: data.source_language || 'und', target_locale: data.locale || data.language,
            source_text: row.text_src, target_text: $('reviewText').value,
            context: row.memory_context || {}, duration: Number($('reviewEnd').value) - Number($('reviewStart').value),
            reviewer: box.querySelector('.memory-reviewer').value,
            meaning_reviewed: meaning, naturalness_reviewed: natural, timing_reviewed: timing,
            status: meaning && natural && timing ? 'reviewed' : 'proposed',
          } });
          box.querySelector('.memory-status').textContent = row.memory_context?.register
            ? 'Saved privately. New jobs can reuse it when all conditions match and reuse is enabled.'
            : 'Saved privately as guidance. This line has no recorded speaker register, so automatic reuse is unavailable.';
        } catch (e) { box.querySelector('.memory-status').textContent = e.message; button.disabled = false; }
      };
    };
  }

  function collect() {
    const row = data.segments.find(s => s.index === selected);
    if (!row) return;
    const extra = scene.collect();
    const patch = { index: selected, cue: row.cue?.cue_id || undefined, text: $('reviewText').value,
      start: Number($('reviewStart').value), end: Number($('reviewEnd').value),
      voice: $('reviewVoice').value, delivery: $('reviewDelivery').value,
      regenerate: $('reviewRegenerate').checked, exclude: $('reviewExclude').checked,
      ...extra };
    const changed = patch.text !== (row.text_translated || row.text_src) || patch.start !== row.start
      || patch.end !== row.end || patch.voice !== (row.voice || '') || patch.delivery !== (row.delivery || '')
      || patch.regenerate || patch.exclude || Object.keys(extra).length > 0;
    if (changed) pending.set(selected, patch); else pending.delete(selected);
    status.textContent = '';
    renderList();
  }
  editor.addEventListener('input', collect);
  list.addEventListener('click', e => {
    const button = e.target.closest('[data-line]');
    if (button) select(Number(button.dataset.line));
  });
  $('reviewFlagged').addEventListener('change', () => {
    const chosen = $('reviewFilter');
    if (chosen) chosen.value = $('reviewFlagged').checked ? 'flagged' : 'all';
    renderList();
  });
  $('reviewFilter')?.addEventListener('change', () => {
    $('reviewFlagged').checked = $('reviewFilter').value !== 'all';
    renderList();
  });
  // Escape out of the editor returns to the list rather than closing the
  // review, so a keyboard user does not lose their place in the episode.
  editor.addEventListener('keydown', event => {
    if (event.key !== 'Escape') return;
    event.stopPropagation();
    event.preventDefault();
    list.querySelector(`[data-line="${selected}"]`)?.focus();
  });
  $('reviewClose').addEventListener('click', () => dialog.close());
  dialog.addEventListener('close', () => {
    scene.reset();
    editor.querySelector('audio')?.pause();
    const target = opener?.isConnected ? opener
      : [...document.querySelectorAll('.job-review')].find(button => button.dataset.id === jobId);
    target?.focus();
  });
  submit.addEventListener('click', async () => {
    const events = scene.events();
    if (!pending.size && !events.length) return;
    const edits = [...pending.values()];
    if (edits.some(e => !Number.isFinite(e.start) || !Number.isFinite(e.end) || e.start < 0
      || e.end <= e.start || !e.text.trim())) {
      status.textContent = 'Each line needs dialogue and an end time after its start.'; return;
    }
    submit.disabled = true;
    status.textContent = 'Queuing changes…';
    try {
      const result = await api(`jobs/${jobId}/review`, { method: 'POST', json: {
        edits, events, use_updated_knowledge: $('reviewUpdatedKnowledge').checked,
        base_revision: data.revision,
      } });
      pending.clear(); data.editable = false; select(selected);
      // Say plainly what was queued: which lines cost new speech, which only
      // re-render, and that everything else is reused.
      status.textContent = `Queued. ${describeRerun(result.rerun)} Close to follow progress.`;
      onQueued();
    } catch (error) { status.textContent = error.message; submit.disabled = false; }
  });

  function describeRerun(plan) {
    if (!plan) return 'Unchanged clips will be reused.';
    const parts = [];
    if (plan.generating) {
      parts.push(`${plan.generating} line${plan.generating === 1 ? '' : 's'} will be generated`);
    }
    if (plan.processing) {
      parts.push(`${plan.processing} will be re-rendered from existing audio`);
    }
    if (plan.candidates) parts.push(`${plan.candidates} alternative takes requested`);
    if (plan.coverage) {
      parts.push(`${plan.coverage} coverage decision${plan.coverage === 1 ? '' : 's'} `
        + 'will be re-mixed without generating speech');
    }
    if (!parts.length) parts.push('nothing needs re-rendering');
    return `${parts.join(', ')}. ${plan.note || ''}`.trim();
  }

  // What the *exported file* was measured to be, kept in its own sentence and
  // deliberately not merged with the review counts. A passing export says the
  // delivered container is structurally what was asked for; it says nothing
  // about whether the dub sounds right, and a summary that blurred the two
  // would be the most misleading line on the screen.
  function deliveryNote(report) {
    if (!report || !report.state) return '';
    const state = {
      passed: 'export checks passed',
      warned: 'export checks passed with warnings',
      failed: 'export checks FAILED',
      unavailable: 'no exported file to check',
      skipped: 'export checks were off',
    }[report.state] || report.state;
    const loudness = report.loudness?.lufs != null
      ? ` · ${report.loudness.lufs.toFixed(1)} LUFS`
        + (report.loudness.true_peak_db != null
          ? `, true peak ${report.loudness.true_peak_db.toFixed(1)} dBFS` : '')
        + (report.profile?.target_lufs == null ? ' (measured, no target set)' : '')
      : '';
    return ` · ${state}${loudness}. Technical checks are not a listening pass.`;
  }

  async function open(id) {
    const version = ++epoch;
    data = null;
    scene.reset();
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
      $('reviewSummary').textContent = `${data.segments.length} lines · `
        + `${data.flagged} flagged for review${deliveryNote(data.delivery)}`;
      $('reviewFlagged').checked = data.flagged > 0;
      const chooser = $('reviewFilter');
      if (chooser) {
        chooser.innerHTML = FILTERS.map(([value, label]) =>
          `<option value="${value}">${esc(label)}</option>`).join('');
        chooser.value = data.flagged > 0 ? 'flagged' : 'all';
      }
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
