import { api, apiUrl } from './api.js';
import { escapeHtml as esc, safeGet } from './dom.js';

// Scope choices offered for a correction, given the refs available in context.
export function scopeOptions({ lineRef = '', titleRef = '', showRef = '' }) {
  const options = [];
  if (lineRef) options.push({ value: 'line', label: 'This line only', ref: lineRef });
  if (titleRef) options.push({ value: 'episode', label: 'This episode / movie', ref: titleRef });
  if (showRef) options.push({ value: 'show', label: 'This show', ref: showRef });
  options.push({ value: 'personal', label: 'My general library', ref: '' });
  return options;
}

export function baseOf(locale) {
  return String(locale || '').split('-')[0].toLowerCase();
}

// Audition a text through the voice-catalog preview mechanism, polling until ready.
export function listen({ key, text, language, audio, status, alive, onDone }) {
  let cancelled = false;
  (async () => {
    status.textContent = 'Generating a short sample…';
    try {
      const response = await api('voice-catalog/preview', {
        method: 'POST', json: { key, text, language },
      });
      const deadline = Date.now() + 180000;
      async function poll() {
        if (cancelled || !alive()) return;
        try {
          const result = await api(`voice-catalog/preview/${response.id}`);
          if (!alive()) return;
          if (['completed', 'done', 'ready', 'success'].includes(result.status)) {
            const apiKey = safeGet('doblarr_api_key', '');
            audio.src = apiUrl(`voice-catalog/preview/${response.id}/audio`)
              + (apiKey ? `?api_key=${encodeURIComponent(apiKey)}` : '');
            audio.hidden = false;
            status.textContent = 'Sample ready.';
            if (onDone) onDone();
          } else if (['failed', 'error', 'cancelled', 'canceled'].includes(result.status)) {
            status.textContent = result.error || 'Voice sample failed.';
            if (onDone) onDone();
          } else if (Date.now() > deadline) {
            status.textContent = 'Still processing in Voicebox.';
            if (onDone) onDone();
          } else setTimeout(poll, 1500);
        } catch (error) { if (alive()) { status.textContent = error.message; if (onDone) onDone(); } }
      }
      poll();
    } catch (error) { status.textContent = error.message; if (onDone) onDone(); }
  })();
  return () => { cancelled = true; };
}

// Shared "add a correction" dialog: draft -> preview against a sentence ->
// optional audition (voice-catalog preview of the resolved text) -> save.
// Saving and re-rendering affected dialogue are separate, explicit actions.
export function openCorrection({
  kind = 'pronunciation', phrase = '', sourceForm = '', locale = 'es',
  text = '', engine = '', voice = '', titleRef = '', showRef = '', lineRef = '',
  onSaved = null,
}) {
  const dialog = document.createElement('dialog');
  dialog.className = 'voice-picker';
  document.getElementById('app').append(dialog);
  const opener = document.activeElement;
  const scopes = scopeOptions({ lineRef, titleRef, showRef });
  const isTerm = kind === 'term';
  dialog.innerHTML = `<header><div><h2>${isTerm ? 'Improve regional wording' : 'Fix pronunciation'}</h2>
    <p class="hint">Saved as a proposed personal rule for ${esc(locale)}; nothing is re-rendered until you ask.</p></div>
    <button class="btn btn-ghost correction-close">Close</button></header>
    <div class="narrator-fields">
      <label>${isTerm ? 'Preferred wording' : 'Written word or phrase'}<input class="input correction-phrase" maxlength="300" value="${esc(phrase)}"></label>
      ${isTerm ? `<label>Source wording (for translation guidance)<input class="input correction-source" maxlength="300" value="${esc(sourceForm)}"></label>`
      : `<label>Replacement spelling<input class="input correction-replacement" maxlength="300" placeholder="How it should be spelled for the speech engine"></label>`}
      <label>Apply this rule<select class="input correction-scope" aria-label="Rule scope">
        ${scopes.map(s => `<option value="${esc(s.value)}">${esc(s.label)}</option>`).join('')}</select></label>
      <label>Sample sentence<textarea class="input correction-text" rows="2" maxlength="2000">${esc(text)}</textarea></label>
    </div>
    <div class="episode-actions">
      <button class="btn btn-secondary correction-preview">Preview matches</button>
      <button class="btn btn-ghost correction-listen" ${voice ? '' : 'disabled'}>Listen</button>
      <button class="btn btn-primary correction-save">Save correction</button>
    </div>
    <p class="correction-preview-out hint"></p>
    <audio controls class="correction-audio" hidden></audio>
    <p class="correction-status" role="status" class="hint"></p>`;
  dialog.showModal();
  dialog.querySelector('.correction-close').onclick = () => dialog.close();
  dialog.addEventListener('close', () => { dialog.querySelector('audio')?.pause(); dialog.remove(); opener?.focus(); });
  const status = dialog.querySelector('.correction-status');
  const out = dialog.querySelector('.correction-preview-out');
  let resolvedText = '';

  function draft() {
    const scope = scopes[dialog.querySelector('.correction-scope').selectedIndex];
    const draftEngine = engine || 'chatterbox';
    return {
      entry: {
        phrase: dialog.querySelector('.correction-phrase').value.trim(),
        kind, locale, source_form: isTerm ? dialog.querySelector('.correction-source').value.trim() : '',
        scope: scope.value, scope_ref: scope.ref,
      },
      realization: isTerm ? null : {
        entry_id: 'draft', engine: draftEngine,
        replacement: dialog.querySelector('.correction-replacement').value.trim() || ' ',
      },
      text: dialog.querySelector('.correction-text').value,
      engine: draftEngine, voice, title_ref: titleRef, show_ref: showRef, line_ref: lineRef,
    };
  }

  dialog.querySelector('.correction-preview').onclick = async () => {
    out.textContent = 'Resolving…';
    try {
      const data = await api('knowledge/preview', { method: 'POST', json: draft() });
      resolvedText = data.after;
      const conflicts = data.conflicts.length
        ? ` Conflicts: ${data.conflicts.map(c => c.reason).join('; ')}.` : '';
      out.textContent = data.before === data.after
        ? `No change to the sample sentence.${conflicts}`
        : `“${data.before}” → “${data.after}”${conflicts}`;
    } catch (error) { out.textContent = error.message; }
  };

  dialog.querySelector('.correction-listen').onclick = async event => {
    const button = event.currentTarget;
    button.disabled = true;
    const sample = resolvedText || dialog.querySelector('.correction-text').value;
    listen({
      key: `profile:${voice}`, text: sample, language: baseOf(locale),
      audio: dialog.querySelector('.correction-audio'), status,
      alive: () => dialog.open, onDone: () => { button.disabled = false; },
    });
  };

  dialog.querySelector('.correction-save').onclick = async event => {
    const button = event.currentTarget;
    button.disabled = true;
    status.textContent = 'Saving…';
    try {
      const { entry, realization } = draft();
      if (!entry.phrase) throw new Error('Enter a word or phrase first.');
      const saved = await api('knowledge/entries', { method: 'POST', json: entry });
      if (!isTerm) {
        const replacement = dialog.querySelector('.correction-replacement').value.trim();
        if (!replacement) throw new Error('Enter a replacement spelling first.');
        await api('knowledge/realizations', {
          method: 'POST',
          json: { ...realization, entry_id: saved.entry.id, replacement },
        });
      }
      status.textContent = 'Saved as proposed. Existing jobs keep their frozen rules until you re-render.';
      if (onSaved) onSaved(saved.entry);
    } catch (error) { status.textContent = error.message; button.disabled = false; }
  };
}
