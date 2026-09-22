import { api } from './api.js';
import { escapeHtml as esc } from './dom.js';
import { SOURCES, createScenePlayer } from './scene-player.js';

// The scene panel inside the review editor: hear the exchange, switch between
// the original and the dub at the same position, inspect a finding, choose a
// take, and set a level — all without leaving the line you are reviewing.

const MODES = [
  ['unknown', 'Not specified'], ['normal', 'Normal speech'], ['thought', 'Inner thought'],
  ['whisper', 'Whisper'], ['shout', 'Shout'], ['call', 'Calling out'],
  ['broadcast', 'Through a speaker'],
];

export const FILTERS = [
  ['all', 'All lines'], ['flagged', 'Any finding'], ['content', 'Wording'],
  ['timing', 'Timing'], ['technical', 'Audio'], ['performance', 'Performance'],
  ['delivery', 'Delivery'],
];

const clock = s => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, '0')}`;

export function matchesFilter(row, filter) {
  if (filter === 'all') return true;
  const findings = row.cue?.findings || [];
  if (filter === 'flagged') return findings.length > 0 || row.issues.length > 0;
  return findings.some(f => f.kind === filter && f.disposition !== 'obsolete');
}

export function createSceneSection({ container, getJobId, getRow, getData, onDecision }) {
  const audio = new Audio();
  audio.preload = 'none';
  const player = createScenePlayer(audio);
  let scene = null, epoch = 0, versions = [], context = 2;

  player.on((event, detail) => {
    const status = container.querySelector('#sceneStatus');
    if (!status) return;
    if (event === 'loading') status.textContent = 'Loading preview…';
    if (event === 'ready') status.textContent = '';
    if (event === 'error') status.textContent = detail.message;
  });

  async function load(row) {
    const version = ++epoch;
    scene = null;
    render(row, 'Loading the surrounding exchange…');
    try {
      scene = await api(`jobs/${getJobId()}/scene/${row.index}?context=${context}`);
      scene.context = context;
      if (version !== epoch) return;
    } catch (error) {
      if (version !== epoch) return;
      render(row, error.message);
      return;
    }
    // Previously saved renders of this dub, so the changed scene can be
    // compared with the one it replaced. A failure here is not fatal: it only
    // means the comparison is unavailable.
    if (!versions.length) {
      try {
        const saved = await api(`jobs/${getJobId()}/versions`);
        if (version !== epoch) return;
        versions = (saved.versions || []).filter(v => v.available && !v.current);
      } catch { versions = []; }
    }
    if (versions.length) player.setVersion(versions[0].version_id);
    player.attach(getJobId(), row.index, scene, row.cue?.level?.applied_db || 0);
    render(row, '');
    // Point the player at the dubbed scene straight away so the control is
    // usable on arrival. `play` only resumes what was already running, so
    // selecting a line never starts making noise on its own.
    await player.play(player.kind, { keepPosition: false });
  }

  function render(row, status) {
    const cue = row.cue || {};
    const available = scene?.available || {};
    container.innerHTML = `
      <div class="scene" role="group" aria-label="Scene playback">
        <div class="scene-tabs" role="tablist" aria-label="What to play">
          ${SOURCES.map(s => `<button type="button" role="tab" class="btn btn-ghost scene-tab"
            data-kind="${s.kind}" aria-selected="${player.kind === s.kind}"
            ${playable(s, available) ? '' : 'disabled'}>${esc(s.label)}</button>`).join('')}
        </div>
        ${versions.length ? `<label class="scene-version">Compare with
          <select id="sceneVersion" class="input">${versions.map(v =>
            `<option value="${esc(v.version_id)}">${esc(v.name)} ·
              ${esc((v.created_at || '').slice(0, 16).replace('T', ' '))}</option>`).join('')}
          </select></label>` : ''}
        <audio id="scenePlayer" controls preload="none"
               aria-label="Scene preview"></audio>
        <div class="scene-controls">
          <label>Context
            <input type="number" id="sceneContext" class="input m" min="0" max="8"
              value="${context}" aria-label="Neighbouring lines to include"></label>
          <label><input type="checkbox" id="sceneLoop"> Loop</label>
          <label><input type="checkbox" id="sceneMatch"
            ${cue.level?.applied_db ? '' : 'disabled'}> Match levels (audition only)</label>
          <span class="hint">${esc(windowNote())}</span>
        </div>
        <p class="hint" id="sceneStatus" role="status">${esc(status || '')}</p>
        ${available.source === false ? `<p class="hint">${esc(
            scene?.available?.note || 'The original audio for this run is not on disk.')}</p>` : ''}
      </div>
      ${takes(cue)}
      ${direction(row, cue)}
      ${findings(row, cue)}`;
    wire(row);
  }

  function playable(source, available) {
    if (source.kind === 'version') return versions.length > 0;
    return available[source.kind] !== false;
  }

  function windowNote() {
    if (!scene) return '';
    const cues = scene.cues?.length || 0;
    const span = scene.target;
    const source = scene.source
      ? ` · original ${clock(scene.source.start)}–${clock(scene.source.end)}` : '';
    return `${cues} line${cues === 1 ? '' : 's'} · ${clock(span.start)}–${clock(span.end)}`
      + `${source} · window grouped by silence, not a detected scene cut`;
  }

  function takes(cue) {
    const rows = scene?.takes || [];
    if (!rows.length) return '';
    return `<fieldset class="scene-takes"><legend>Takes</legend>
      ${rows.map(t => `<label class="scene-take ${t.available ? '' : 'is-missing'}">
        <input type="radio" name="sceneTake" value="${esc(t.take_id)}"
          ${t.selected ? 'checked' : ''} ${t.available ? '' : 'disabled'}>
        <span><strong>${esc(t.origin === 'candidate' ? 'Alternative' : 'Original take')}</strong>
        <span class="m">${esc(t.take_id)}</span>
        <span class="hint">${esc(takeNote(t))}</span></span>
        <button type="button" class="btn btn-ghost scene-audition" data-take="${esc(t.take_id)}"
          ${t.available ? '' : 'disabled'}>Audition</button></label>`).join('')}
      <p class="hint">Technical checks explain defects. They are not a judgement about
        the acting — choosing a take is yours.</p></fieldset>`;
  }

  function takeNote(take) {
    if (take.state === 'failed') return `failed: ${take.error || 'no reason recorded'}`;
    if (!take.available) return 'audio is no longer on disk';
    const checks = take.checks || {};
    if (checks.state === 'defective') return `checks: ${(checks.defects || []).join(', ')}`;
    if (checks.duration) return `${checks.duration.toFixed(2)}s · ${checks.rms_db} dB`;
    return take.direction ? `directed: ${take.direction}` : 'no technical checks recorded';
  }

  function direction(row, cue) {
    const intent = cue.intent || {};
    const level = cue.level || {};
    const measurement = cue.measurement || {};
    return `<fieldset class="scene-direction"><legend>Performance</legend>
      <label class="review-field">Speech mode<select id="sceneMode" class="input">
        ${MODES.map(([value, label]) => `<option value="${value}"
          ${(intent.mode || 'unknown') === value ? 'selected' : ''}>${esc(label)}</option>`)
          .join('')}</select></label>
      <label class="review-field">Delivery traits
        <input id="sceneTraits" class="input" maxlength="200"
          value="${esc((intent.traits || []).join(', '))}"
          placeholder="urgent, restrained"></label>
      <label class="review-field">Line direction
        <input id="sceneDirection" class="input" maxlength="500"
          value="${esc(intent.direction || '')}" placeholder="hold back, almost out of breath">
      </label>
      <p class="hint">${esc(intentNote(intent))}</p>
      <div class="review-timing">
        <label class="review-field">Gain (dB)
          <input id="sceneGain" class="input m" type="number" step="0.5" min="-24" max="24"
            value="${cue.review_gain ?? ''}" placeholder="${level.applied_db ?? 0}"></label>
        <label class="review-field">Alternative takes
          <input id="sceneCandidates" class="input m" type="number" min="0"
            max="${getData()?.candidate_limit ?? 4}" value="0"></label>
      </div>
      <p class="hint">${esc(levelNote(level, measurement))}</p></fieldset>`;
  }

  function intentNote(intent) {
    if (!intent.effective) return 'No delivery direction has been applied to this line.';
    // Which layer contributed each part, so an inherited direction and this
    // line's own override are told apart rather than blurred into one string.
    const layers = (intent.sources || [])
      .map(s => `${s.origin}: ${s.text}`).join(' · ');
    const from = layers ? ` From ${layers}.` : '';
    if (intent.capability === 'supported') return `Applied: ${intent.effective}.${from}`;
    if (intent.capability === 'unsupported') {
      return `Asked for "${intent.effective}" — this engine does not accept delivery `
        + `instructions, so it was not applied.${from}`;
    }
    return `Composed "${intent.effective}" — this engine's capability is unknown.${from}`;
  }

  function levelNote(level, measurement) {
    if (!level.mode || level.mode === 'legacy') {
      return 'Levels: the pre-timing loudness pass owns this run.';
    }
    const parts = [`Levels: ${level.mode}`];
    if (level.outcome === 'fallback') parts.push(`fell back — ${level.reason}`);
    else if (level.outcome === 'clamped') parts.push(level.reason);
    else if (level.applied_db) parts.push(`${level.applied_db > 0 ? '+' : ''}${level.applied_db} dB`);
    else parts.push('no performance gain needed');
    if (measurement.state && measurement.state !== 'measured') {
      parts.push(`source evidence: ${measurement.state}`);
    } else if (measurement.relative_db != null) {
      parts.push(`original was ${measurement.relative_db > 0 ? '+' : ''}`
        + `${measurement.relative_db} dB vs ordinary dialogue`);
    }
    if (level.peak_limited) parts.push('held back by the peak ceiling');
    return parts.join(' · ');
  }

  function findings(row, cue) {
    const rows = (cue.findings || []).filter(f => f.disposition !== 'obsolete');
    const verification = cue.verification || {};
    const words = verification.state && verification.state !== 'unknown'
      ? `<p class="hint">${esc(verificationNote(verification))}</p>` : '';
    if (!rows.length) return `<div class="scene-findings">${words}</div>`;
    return `<fieldset class="scene-findings"><legend>Findings</legend>${words}
      ${rows.map(f => `<div class="scene-finding" data-finding="${esc(f.finding_id)}">
        <p><strong>${esc(f.code.replaceAll('_', ' '))}</strong>
          <span class="m">${esc(f.kind)} · ${esc(f.severity)}</span>
          ${f.review?.stale ? '<span class="review-marker">earlier verdict — audio has changed</span>' : ''}
        </p>
        <details><summary>Evidence</summary><pre class="m">${esc(evidence(f))}</pre></details>
        <div class="review-options">
          <label>Verdict<select class="input scene-disposition">
            ${['open', 'accepted', 'fixed'].map(d => `<option value="${d}"
              ${f.disposition === d ? 'selected' : ''}>${d}</option>`).join('')}
          </select></label>
          <input class="input scene-note" maxlength="2000" placeholder="Why?"
            value="${esc(f.review?.note || '')}">
        </div></div>`).join('')}
      <button type="button" class="btn btn-secondary" id="sceneSaveVerdicts">Save verdicts</button>
      <p class="hint" id="sceneVerdictStatus" role="status"></p>
      <p class="hint">A rendered export never marks a scene reviewed. These verdicts are
        recorded against this exact version of the audio.</p></fieldset>`;
  }

  function verificationNote(verification) {
    const state = {
      match: 'Recognition matched the requested words.',
      mismatch: 'Recognition did not match the requested words.',
      uncertain: 'Recognition differs, but not enough to prove a wrong word.',
      empty: 'Recognition returned nothing; this line is unverified.',
      failed: 'The recognizer failed; this line is unverified.',
      skipped: 'Not checked.',
      unsupported: 'This language is not supported by the checker.',
    }[verification.state] || 'Not checked.';
    const detail = verification.reason ? ` ${verification.reason}.` : '';
    const diff = (verification.differences || []).slice(0, 4)
      .map(d => `${d.op} “${(d.expected || d.heard || []).join(' ')}”`).join(', ');
    // How many bounded repairs this line already cost, so a reviewer can see
    // that the pipeline tried and stopped rather than never trying.
    const tries = verification.attempts
      ? ` ${verification.attempts} automatic repair${verification.attempts === 1 ? '' : 's'} `
        + 'were already spent on this line.'
      : '';
    const heard = verification.heard
      ? ` Heard: “${verification.heard}”.` : '';
    return `Words: ${state}${detail}${heard}${diff ? ` Differences: ${diff}.` : ''}${tries}`;
  }

  function evidence(finding) {
    const data = { ...finding.evidence };
    delete data.differences;
    const diff = (finding.evidence?.differences || [])
      .map(d => `${d.op} at ${d.at}: expected ${JSON.stringify(d.expected)} heard ${JSON.stringify(d.heard)}`);
    return [JSON.stringify(data, null, 1), ...diff].join('\n');
  }

  function wire(row) {
    const el = id => container.querySelector(id);
    const holder = el('#scenePlayer');
    if (holder) holder.replaceWith(audio);
    audio.id = 'scenePlayer';
    audio.controls = true;
    audio.setAttribute('aria-label', 'Scene preview');
    container.querySelectorAll('.scene-tab').forEach(button => {
      button.onclick = async () => {
        await player.play(button.dataset.kind);
        container.querySelectorAll('.scene-tab').forEach(other => {
          other.setAttribute('aria-selected', String(other === button));
        });
      };
    });
    container.querySelectorAll('.scene-audition').forEach(button => {
      button.onclick = () => player.play('take', { keepPosition: false,
        takeId: button.dataset.take });
    });
    const chooser = el('#sceneVersion');
    if (chooser) {
      chooser.value = player.version || chooser.value;
      chooser.onchange = event => {
        player.setVersion(event.target.value);
        if (player.kind === 'version') player.play('version');
      };
    }
    const width = el('#sceneContext');
    if (width) {
      width.onchange = event => {
        const wanted = Math.max(0, Math.min(8, Number(event.target.value) || 0));
        if (wanted === context) return;
        context = wanted;
        load(row);
      };
    }
    if (el('#sceneLoop')) el('#sceneLoop').onchange = e => player.setLoop(e.target.checked);
    if (el('#sceneMatch')) el('#sceneMatch').onchange = e => player.setMatched(e.target.checked);
    const verdicts = el('#sceneSaveVerdicts');
    if (verdicts) verdicts.onclick = () => saveVerdicts(row, verdicts);
  }

  async function saveVerdicts(row, button) {
    const status = container.querySelector('#sceneVerdictStatus');
    const dispositions = [...container.querySelectorAll('.scene-finding')].map(node => ({
      finding: node.dataset.finding,
      disposition: node.querySelector('.scene-disposition').value,
      note: node.querySelector('.scene-note').value,
    }));
    button.disabled = true;
    status.textContent = 'Saving…';
    try {
      await api(`jobs/${getJobId()}/decisions`, { method: 'POST', json: {
        cue: row.cue?.cue_id, base_revision: getData()?.revision, dispositions,
      } });
      status.textContent = 'Saved against this version of the audio.';
      onDecision?.();
    } catch (error) {
      status.textContent = error.message;
    } finally {
      button.disabled = false;
    }
  }

  function collect() {
    const el = id => container.querySelector(id);
    if (!el('#sceneMode')) return {};
    const patch = {};
    const mode = el('#sceneMode').value;
    const traits = el('#sceneTraits').value.split(',').map(t => t.trim()).filter(Boolean);
    const direction = el('#sceneDirection').value;
    const gain = el('#sceneGain').value;
    const candidates = Number(el('#sceneCandidates').value) || 0;
    const chosen = container.querySelector('input[name="sceneTake"]:checked');
    const cue = getRow()?.cue || {};
    if (mode !== (cue.intent?.mode || 'unknown')) patch.mode = mode;
    if (traits.join(',') !== (cue.intent?.traits || []).join(',')) patch.traits = traits;
    if (direction !== (cue.intent?.direction || '')) patch.direction = direction;
    if (gain !== '') patch.gain_db = Number(gain);
    if (candidates) patch.candidates = candidates;
    if (chosen && chosen.value !== scene?.selection?.take_id) patch.take = chosen.value;
    return patch;
  }

  return {
    load,
    collect,
    stop() { player.stop(); },
    reset() {
      epoch += 1; scene = null; versions = []; context = 2;
      player.stop(); container.innerHTML = '';
    },
  };
}
