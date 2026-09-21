import { api } from './api.js';
import { castParams } from './identity.js';
import { escapeHtml } from './dom.js';

export function recipeMedia(item) {
  return { kind: item.episode_id ? 'episode' : 'movie', title: item.parent?.title || item.title,
    tmdb_id: item.tmdb_id || null, tvdb_id: item.tvdb_id || null,
    season: item.season ?? null, episode: item.episode_number ?? null,
    runtime_seconds: null };
}

export function renderRecipes(root, { item, target, onApplied }) {
  root.className = 'panel';
  root.innerHTML = `<h3>Dub recipes</h3>
    <p class="hint">Share the settings for a dub. Each person generates it using their own media and locally available voices.</p>
    <p class="hint">Recipe files contain no audio, video, subtitles, or cloned voice recordings. Dialogue and timing are extracted again from the local file.</p>
    <div class="narrator-fields">
      <label>Creator<input class="input" id="recipeCreator" maxlength="100" autocomplete="off"></label>
      <label>Recipe version<input class="input" id="recipeRevision" value="1" maxlength="40"></label>
      <label>Expected runtime (seconds)<input class="input" id="recipeRuntime" type="number" min="1" max="86400" step="0.001" placeholder="Unknown"></label>
      <label class="narrator-direction">Release notes<textarea class="input" id="recipeNotes" maxlength="2000" rows="3" placeholder="Edition, delivery style, or casting notes"></textarea></label>
    </div>
    <p class="hint">Export uses saved settings and character assignments. Review the file before sharing; names and notes are included.</p>
    <div class="episode-actions"><button class="btn btn-secondary" id="recipeExport">Export recipe</button>
      <label class="btn btn-secondary" for="recipeFile">Import recipe</label>
      <input id="recipeFile" aria-label="Import recipe file" type="file" accept=".dobdub,application/json" style="max-width:100%">
    </div><p id="recipeStatus" role="status" class="hint"></p><div id="recipePreview"></div>`;
  const status = root.querySelector('#recipeStatus');
  const previewRoot = root.querySelector('#recipePreview');
  const identity = Object.fromEntries(castParams(item));
  const media = recipeMedia(item);
  let fileRequest = 0;
  const exportButton = root.querySelector('#recipeExport');
  exportButton.onclick = async () => {
    exportButton.disabled = true; status.textContent = 'Preparing recipe…';
    try {
      const runtime = root.querySelector('#recipeRuntime');
      if (!runtime.checkValidity()) throw new Error('Enter a runtime between 1 and 86400 seconds, or leave it blank.');
      const data = await api('recipes/export', { method:'POST', json: {
        identity, media: { ...media, runtime_seconds: runtime.value ? Number(runtime.value) : null },
        parent: item.parent ? Object.fromEntries(castParams(item.parent)) : null,
        source_language: item.original || '', target_language: target,
        creator: root.querySelector('#recipeCreator').value,
        revision: root.querySelector('#recipeRevision').value,
        notes: root.querySelector('#recipeNotes').value,
      } });
      if (!root.isConnected) return;
      const blob = new Blob([JSON.stringify(data.recipe, null, 2) + '\n'], { type:'application/json' });
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = `${(item.title || 'dub').replace(/[^a-z0-9-]/gi, '-').slice(0,100)}-${data.recipe.target_language}.dobdub`;
      link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
      status.textContent = ['Recipe exported.', ...data.warnings].join(' ');
    } catch (e) { status.textContent = e.message; }
    finally { exportButton.disabled = false; }
  };
  root.querySelector('#recipeFile').onchange = async event => {
    const request = ++fileRequest;
    previewRoot.replaceChildren();
    const file = event.target.files[0];
    if (!file) return;
    status.textContent = 'Checking recipe…';
    try {
      if (file.size > 128 * 1024) throw new Error('Recipe files must be smaller than 128 KB. Audio and video are not supported.');
      const recipe = JSON.parse(await file.text());
      const data = await api('recipes/preview', { method:'POST', json:{ identity, media, recipe } });
      if (!root.isConnected || request !== fileRequest) return;
      status.textContent = 'Recipe checked. Choose local voices before applying.';
      showPreview(data);
    } catch (e) { if (request === fileRequest) status.textContent = e instanceof SyntaxError ? 'This is not a valid JSON recipe file.' : e.message; }
  };
  function showPreview(data) {
    const r = data.recipe;
    const slots = [{ key:'narrator', label:'Narrator', voice:r.narrator },
      ...r.characters.map(c => ({ key:'character:' + c.speaker_id, label:`${c.label} (${c.speaker_id})`, voice:c.voice }))];
    previewRoot.innerHTML = `<hr><h3>Review recipe</h3>
      <p><strong>${escapeHtml(r.media.title)}</strong> · ${escapeHtml(r.target_language)} · version ${escapeHtml(r.revision)}</p>
      <p class="hint">${r.creator ? `By ${escapeHtml(r.creator)}` : 'No creator specified'}</p>
      <p style="white-space:pre-wrap;overflow-wrap:anywhere">${escapeHtml(r.notes)}</p>
      ${data.warnings.map(w => `<p class="hint">${escapeHtml(w)}</p>`).join('')}
      <div class="narrator-fields">${slots.map((s,i) => `<label>${escapeHtml(s.label)} — ${escapeHtml(s.voice.engine)}
        <span class="hint">Requested: ${escapeHtml(s.voice.name || 'Local source audio')}${s.voice.delivery ? ` · ${escapeHtml(s.voice.delivery)}` : ''}</span>
        <select class="input" data-slot="${i}" aria-label="Local voice for ${escapeHtml(s.label)}">
          <option value="">Use local source audio</option>
          ${data.voices.map(v => `<option value="${escapeHtml(v.id)}">${escapeHtml(v.name)}</option>`).join('')}
        </select><select class="input" data-engine="${i}" aria-label="Local engine for ${escapeHtml(s.label)}">
          ${['chatterbox','chatterbox_turbo','qwen','qwen_custom_voice','kokoro','luxtts','tada'].map(e => `<option value="${e}">${e}</option>`).join('')}
        </select></label>`).join('')}</div>
      <p class="hint">Source-audio casting needs a cloning engine; missing preset voices default to Qwen. Delivery directions require Qwen. Check your local voice and engine compatibility.</p>
      <details><summary>Included generation settings (${Object.keys(r.settings).length})</summary><pre style="white-space:pre-wrap;overflow-wrap:anywhere">${escapeHtml(JSON.stringify(r.settings,null,2))}</pre></details>
      <p class="hint">Applying replaces this title’s character cast and included settings, and clears prior dialogue edits and shared cast links. Existing jobs and global settings stay unchanged. Review speaker assignments before queueing a dub.</p>
      <button class="btn btn-primary" id="recipeApply">Apply recipe to this ${media.kind}</button>`;
    slots.forEach((s,i) => {
      const engine = previewRoot.querySelector(`[data-engine="${i}"]`);
      engine.value = s.voice.engine;
      const matches = data.voices.filter(v => s.voice.name && v.name === s.voice.name);
      const choice = previewRoot.querySelector(`[data-slot="${i}"]`);
      if (matches.length === 1) choice.value = matches[0].id;
      const adapt = () => { if (!choice.value && ['kokoro','qwen_custom_voice'].includes(engine.value)) engine.value = 'qwen'; };
      choice.onchange = adapt; adapt();
    });
    previewRoot.querySelector('#recipeApply').onclick = async event => {
      const button = event.currentTarget; button.disabled = true;
      root.querySelector('#recipeFile').disabled = true;
      status.textContent = 'Applying recipe…';
      try {
        const voices = Object.fromEntries(slots.map((s,i) => [s.key, previewRoot.querySelector(`[data-slot="${i}"]`).value]));
        const engines = Object.fromEntries(slots.map((s,i) => [s.key, previewRoot.querySelector(`[data-engine="${i}"]`).value]));
        const result = await api('recipes/import', {method:'POST', json:{identity, media, recipe:r, voices, engines}});
        if (!root.isConnected) return;
        onApplied(result.plan);
        status.textContent = 'Recipe applied. Review Speakers & voices, then queue a dub when ready. Nothing has been generated.';
        previewRoot.replaceChildren();
      } catch (e) { status.textContent = e.message; button.disabled = false; }
      finally { root.querySelector('#recipeFile').disabled = false; }
    };
  }
}
