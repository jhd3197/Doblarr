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
const seconds = s => (s == null ? '—' : `${Number(s).toFixed(2)}s`);

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
  // Timing and coverage decisions live here between renders of the panel,
  // keyed by line and by event, so moving to another line does not quietly
  // drop one a reviewer already made.
  const pendingTiming = new Map();
  const pendingEvents = new Map();
  const pendingSpace = new Map();

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
      ${timing(row, cue)}
      ${space(row)}
      ${coverage()}
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

  // --- phrase timing ------------------------------------------------------
  //
  // A reviewer's unit of work here is a phrase and a pause, not a filter graph.
  // The panel shows where each phrase landed, lets one be anchored to a moment,
  // lets a pause be marked as performance so fitting stops spending it, and
  // offers a bypass for the line where none of that is wanted. Every one of
  // those re-renders the take that already exists; none generates speech.

  function timing(row, cue) {
    const plan = scene?.phrasing;
    if (!plan || plan.mode === 'unknown') return '';
    if (plan.mode === 'whole') {
      return `<fieldset class="scene-timing"><legend>Timing</legend>
        <p class="hint">Whole-clip fitting owns this run: a line that overruns is
          compressed evenly end to end. Set the timing approach to "by phrase" in
          settings to fit the parts instead.</p></fieldset>`;
    }
    const edit = pendingTiming.get(row.index) || scene?.timing_edit || {};
    const anchors = new Map((edit.anchors || []).map(a => [a.phrase || '', a]));
    const pauses = new Map(Object.entries(edit.pauses || {}));
    return `<fieldset class="scene-timing"><legend>Timing</legend>
      <p class="hint">${esc(timingNote(plan))}</p>
      ${plan.conflicts?.length ? `<ul class="scene-conflicts">${plan.conflicts
        .map(c => `<li>${esc(c.detail || c.code)}</li>`).join('')}</ul>` : ''}
      <ol class="scene-phrases">${(plan.phrases || []).map(p => `
        <li data-phrase="${esc(p.phrase_id)}">
          <span class="m">${seconds(p.at)}</span>
          <span>${esc(p.text || `phrase ${p.order + 1}`)}</span>
          <label class="m">Anchor at
            <input type="number" class="input m scene-anchor" step="0.05" min="0"
              max="${Number(plan.slot ?? 0).toFixed(2)}"
              value="${anchors.get(p.phrase_id)?.at ?? ''}"
              placeholder="${p.at == null ? '' : Number(p.at).toFixed(2)}"
              aria-label="Anchor this phrase, seconds after the line starts"></label>
          ${anchorNote(plan, p.phrase_id)}
        </li>`).join('')}</ol>
      ${(plan.pauses || []).filter(p => p.origin !== 'boundary').length
        ? `<div class="scene-pauses"><p class="m">Pauses</p>${(plan.pauses || [])
          .filter(p => p.origin !== 'boundary').map(p => `
          <label class="scene-pause"><input type="checkbox" class="scene-protect"
            data-pause="${esc(p.pause_id)}"
            ${(pauses.get(p.pause_id)?.protected ?? p.protected) ? 'checked' : ''}>
            ${esc(pauseNote(p))}</label>`).join('')}</div>` : ''}
      <label><input type="checkbox" id="sceneBypass" ${edit.bypass ? 'checked' : ''}>
        Leave this line's timing exactly as generated</label>
      ${collisions(edit)}
      <p class="hint">Changing an anchor or a pause re-renders this line from the take it
        already has. No new speech is generated.</p></fieldset>`;
  }

  function timingNote(plan) {
    const state = {
      applied: 'Fitted by phrase.',
      fallback: 'Fitted as one bounded whole.',
      infeasible: 'These words do not fit this window.',
      bypassed: 'Left exactly as generated.',
      unavailable: 'No timing plan could be made.',
      planned: 'Planned, not yet rendered.',
    }[plan.state] || 'Timing not recorded.';
    const sizes = plan.actual_duration != null && plan.slot != null
      ? ` ${plan.actual_duration.toFixed(2)}s in a ${plan.slot.toFixed(2)}s window.` : '';
    return `${state} ${plan.reason || ''}${sizes}`.trim();
  }

  function anchorNote(plan, phraseId) {
    const anchor = (plan.anchors || []).find(a => a.phrase_id === phraseId
      && a.kind === 'hard' && a.error != null);
    if (!anchor) return '';
    const off = Math.abs(anchor.error);
    if (off <= (anchor.tolerance ?? 0.12)) return '<span class="hint">landed on time</span>';
    return `<span class="review-marker">landed ${off.toFixed(2)}s `
      + `${anchor.error > 0 ? 'late' : 'early'}</span>`;
  }

  function pauseNote(pause) {
    const length = pause.clip ? (pause.clip.end - pause.clip.start) : 0;
    const planned = pause.planned == null ? '' : ` to ${pause.planned.toFixed(2)}s`;
    const kind = pause.protected ? 'kept as performance' : 'available as padding';
    return `${length.toFixed(2)}s${planned} · ${kind}`;
  }

  function collisions(edit) {
    const rows = scene?.collisions || [];
    if (!rows.length) return '';
    const accepted = edit.overlap != null ? !!edit.overlap
      : rows.some(r => r.accepted && !r.accepted.stale);
    return `<div class="scene-collisions">${rows.map(r => `<p>
      <strong>${esc(collisionLabel(r.code))}</strong>
      <span class="m">line ${r.with_line} · ${r.seconds}s</span>
      <span class="hint">${esc(r.note || '')}</span>
      ${r.accepted?.stale
        ? '<span class="review-marker">earlier decision — the audio has changed</span>'
        : ''}</p>`).join('')}
      <label><input type="checkbox" id="sceneOverlap" ${accepted ? 'checked' : ''}>
        This overlap is deliberate</label></div>`;
  }

  function collisionLabel(code) {
    return {
      timing_collision: 'Introduced collision',
      timing_self_overlap: 'Talking over themselves',
      timing_overlap_intended: 'Original overlap kept',
      timing_overlap_accepted: 'Accepted as deliberate',
    }[code] || code.replaceAll('_', ' ');
  }

  // --- scene space and device treatments ----------------------------------
  //
  // A reviewer's unit of work here is "where does this line sound like it is",
  // not a filter graph. The panel shows the preset that was chosen and where
  // the choice came from, offers the small supported set, and offers a bypass
  // for the line where none of it is wanted. A preset this FFmpeg build cannot
  // render is shown as unavailable rather than offered and then refused.

  function space(row) {
    const treatment = scene?.treatment;
    if (!treatment) return '';
    const policy = getData()?.treatments || {};
    if (policy.mode !== 'on' && treatment.outcome !== 'applied') {
      return `<fieldset class="scene-space"><legend>Space</legend>
        <p class="hint">Acoustic treatment is off for this run, so every line is
          placed dry. Turn it on in settings to put a room, a distance or a
          device around a line.</p></fieldset>`;
    }
    const catalogue = policy.catalogue || [];
    const edit = pendingSpace.get(row.index) || scene?.treatment_edit || {};
    const chosen = edit.bypass ? 'dry' : (edit.preset || treatment.preset || 'dry');
    const intensity = edit.intensity ?? treatment.intensity ?? policy.intensity ?? 1;
    return `<fieldset class="scene-space"><legend>Space</legend>
      <p class="hint">${esc(spaceNote(treatment))}</p>
      <div class="review-options">
        <label class="review-field">Preset<select id="sceneTreatment" class="input">
          ${(catalogue.length ? catalogue : [{ preset: chosen, capability: 'unknown' }])
            .map(p => `<option value="${esc(p.preset)}"
              ${p.preset === chosen ? 'selected' : ''}
              ${p.capability === 'unsupported' ? 'disabled' : ''}>${esc(p.preset)}${
                p.capability === 'unsupported'
                  ? ` — unavailable (needs ${esc((p.missing || []).join(', '))})` : ''
              }</option>`).join('')}
        </select></label>
        <label class="review-field">Intensity
          <input id="sceneIntensity" class="input m" type="number" step="0.1"
            min="0" max="1" value="${Number(intensity).toFixed(1)}"></label>
      </div>
      <label><input type="checkbox" id="sceneNoSpace" ${edit.bypass ? 'checked' : ''}>
        Leave this line dry whatever the scene says</label>
      ${(scene?.treatment_findings || []).length
        ? `<ul class="scene-conflicts">${scene.treatment_findings.map(f =>
            `<li>${esc(f.code.replaceAll('_', ' '))}: ${esc(f.note || '')}</li>`).join('')}</ul>`
        : ''}
      ${catalogue.length ? `<details><summary>What each preset does</summary>
        <ul class="hint">${catalogue.map(p =>
          `<li><strong>${esc(p.preset)}</strong> — ${esc(p.summary || '')}</li>`).join('')}
        </ul></details>` : ''}
      <p class="hint">Changing the space re-renders this line from the take it already
        has. No new speech is generated, and the dry line is kept so the choice is
        reversible.</p></fieldset>`;
  }

  function spaceNote(treatment) {
    const origin = { default: 'the run default', scene: 'a scene rule',
      line: 'a choice made for this line', manual: 'a manual choice',
      none: 'nothing' }[treatment.origin] || treatment.origin;
    if (treatment.outcome === 'applied') {
      const tail = treatment.tail
        ? `, ringing out for ${treatment.tail.toFixed(2)}s past the words` : '';
      const makeup = treatment.makeup
        ? ` The level was corrected by ${treatment.makeup > 0 ? '+' : ''}`
          + `${treatment.makeup.toFixed(1)} dB so the effect did not change it.` : '';
      return `Playing through ${treatment.preset}, chosen by ${origin}${tail}.${makeup}`;
    }
    if (treatment.outcome === 'unsupported') {
      return `${treatment.preset} was asked for by ${origin} and this FFmpeg build `
        + `cannot render it (missing ${(treatment.missing || []).join(', ')}), so the `
        + `line is dry.`;
    }
    if (treatment.outcome === 'bypassed') {
      return treatment.reason || 'No treatment was selected; the line is dry.';
    }
    if (treatment.outcome === 'unavailable' || treatment.outcome === 'failed') {
      return `Asked for ${treatment.preset} and it could not be rendered: `
        + `${treatment.reason || 'no reason recorded'}.`;
    }
    return 'No acoustic treatment has been recorded for this line.';
  }

  // --- reaction and background coverage -----------------------------------

  function coverage() {
    const rows = scene?.events || [];
    const bed = scene?.available?.bed_note || '';
    if (!rows.length) {
      return `<fieldset class="scene-coverage"><legend>Coverage</legend>
        <p class="hint">No reaction or background event was recorded in this window.
          ${esc(bed)}</p></fieldset>`;
    }
    return `<fieldset class="scene-coverage"><legend>Coverage</legend>
      <p class="hint">${esc(bed)}</p>
      ${rows.map(e => `<div class="scene-event" data-event="${esc(e.event_id)}">
        <p><strong>${esc(e.type)}</strong>
          <span class="m">${seconds(e.target?.start)} · ${esc(e.category)}</span>
          <span class="hint">${esc(e.text || '')}</span></p>
        <p class="hint">${esc(eventNote(e))}</p>
        ${(e.findings || []).map(f => `<div class="scene-event-finding"
            data-finding="${esc(f.finding_id)}">
          <p class="review-flags">${esc(f.code.replaceAll('_', ' '))}:
            ${esc(f.evidence?.note || '')}
            ${f.review?.stale
              ? '<span class="review-marker">earlier verdict — the audio has changed</span>'
              : ''}</p>
          <label class="m">Verdict<select class="input scene-event-disposition">
            ${['open', 'accepted', 'fixed'].map(d => `<option value="${d}"
              ${f.disposition === d ? 'selected' : ''}>${d}</option>`).join('')}
          </select></label></div>`).join('')}
        <div class="review-options">
          <label>Coverage<select class="input scene-decision">
            ${['unresolved', 'retain', 'replace', 'omit', 'covered'].map(d =>
              `<option value="${d}" ${e.decision === d ? 'selected' : ''}>${d}</option>`)
              .join('')}</select></label>
          <input class="input scene-asset" maxlength="1000"
            placeholder="Replacement sound file" value="${esc(e.asset || '')}">
          <button type="button" class="btn btn-ghost scene-hear-event"
            data-event="${esc(e.event_id)}" ${e.playable ? '' : 'disabled'}>Hear it</button>
        </div></div>`).join('')}
      <p class="hint">Nothing is inserted without a decision. A coverage change re-mixes;
        it never generates speech.</p>
      <div class="review-options">
        <input class="input" id="sceneCoverageNote" maxlength="2000"
          placeholder="Note about the reactions or the bed in this scene"
          value="${esc(rows[0]?.decision?.note || '')}">
        <button type="button" class="btn btn-secondary" id="sceneSaveCoverage">
          Save coverage notes</button>
      </div>
      <p class="hint" id="sceneCoverageStatus" role="status"></p></fieldset>`;
  }

  function eventNote(event) {
    const state = {
      unresolved: 'Not decided. A subtitle tag proves neither that the sound is missing '
        + 'nor that it survived.',
      retained: 'The original sound is placed here.',
      replaced: 'A supplied sound is placed here.',
      omitted: 'Left out on purpose.',
      covered: 'Already carried by the background.',
      unavailable: 'Asked for, but there is nothing to place.',
      unsupported: 'Asked for, and this engine cannot produce it.',
    }[event.coverage] || 'Not decided.';
    return `${state}${event.reason ? ` ${event.reason}.` : ''}`;
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
    container.querySelectorAll('.scene-hear-event').forEach(button => {
      button.onclick = () => player.play('event', { keepPosition: false,
        eventId: button.dataset.event });
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
    const coverageNotes = el('#sceneSaveCoverage');
    if (coverageNotes) coverageNotes.onclick = () => saveCoverage(coverageNotes);
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

  // What the timing controls are asking for, or nothing when this line has no
  // phrase plan on screen. Returning the anchors and pauses only when they
  // differ from what the run already decided keeps an untouched line out of the
  // re-render entirely.
  function collectTiming(row) {
    const plan = scene?.phrasing;
    if (!plan || plan.mode !== 'phrase') return {};
    const before = scene?.timing_edit || {};
    const patch = {};
    const anchors = [...container.querySelectorAll('.scene-phrases li')]
      .map(node => ({ phrase: node.dataset.phrase,
        at: node.querySelector('.scene-anchor')?.value }))
      .filter(a => a.at !== '' && a.at != null)
      .map(a => ({ phrase: a.phrase, edge: 'start', at: Number(a.at) }))
      .filter(a => Number.isFinite(a.at));
    const wasAnchors = (before.anchors || [])
      .map(a => `${a.phrase}:${a.at}`).sort().join('|');
    if (anchors.map(a => `${a.phrase}:${a.at}`).sort().join('|') !== wasAnchors) {
      patch.anchors = anchors;
    }
    const pauses = [...container.querySelectorAll('.scene-protect')].map(node => ({
      pause: node.dataset.pause, protected: node.checked,
      kind: node.checked ? 'pause' : 'padding',
    }));
    const planned = new Map((plan.pauses || []).map(p => [p.pause_id, p.protected]));
    const overrides = new Map(Object.entries(before.pauses || {}));
    const changed = pauses.filter(p => {
      const current = overrides.has(p.pause) ? overrides.get(p.pause).protected
        : planned.get(p.pause);
      return p.protected !== current;
    });
    if (changed.length) patch.pauses = pauses.filter(p => p.protected !== planned.get(p.pause));
    const bypass = container.querySelector('#sceneBypass');
    if (bypass && bypass.checked !== !!before.bypass) patch.bypass_timing = bypass.checked;
    const overlap = container.querySelector('#sceneOverlap');
    if (overlap) {
      const was = before.overlap != null ? !!before.overlap
        : (scene?.collisions || []).some(r => r.accepted && !r.accepted.stale);
      if (overlap.checked !== was) patch.overlap = overlap.checked;
    }
    if (Object.keys(patch).length) pendingTiming.set(row.index, {
      ...(pendingTiming.get(row.index) || {}), ...patch });
    else pendingTiming.delete(row.index);
    return patch;
  }

  // What the space controls are asking for, or nothing when this line's space
  // is already what the run decided. A bypass is kept as a bypass rather than
  // rewritten as "dry": it says *this line* is to be left alone, and it has to
  // survive a change to the scene rule above it.
  function collectSpace(row) {
    const select = container.querySelector('#sceneTreatment');
    if (!select || !scene?.treatment) return {};
    const before = scene?.treatment_edit || {};
    const bypass = container.querySelector('#sceneNoSpace')?.checked || false;
    const preset = select.value;
    const intensity = Number(container.querySelector('#sceneIntensity')?.value);
    if (bypass) {
      if (before.bypass) { pendingSpace.delete(row.index); return {}; }
      pendingSpace.set(row.index, { bypass: true });
      return { treatment: { bypass: true } };
    }
    const wasPreset = before.bypass ? 'dry' : (before.preset || scene.treatment.preset);
    const wasIntensity = before.intensity ?? scene.treatment.intensity;
    const movedPreset = preset !== wasPreset;
    const movedIntensity = Number.isFinite(intensity)
      && Math.abs(intensity - Number(wasIntensity ?? 1)) > 1e-6;
    if (!movedPreset && !movedIntensity && !before.bypass) {
      pendingSpace.delete(row.index);
      return {};
    }
    const patch = { preset };
    if (Number.isFinite(intensity)) patch.intensity = intensity;
    pendingSpace.set(row.index, patch);
    return { treatment: patch };
  }

  // Coverage decisions belong to the run, not to the line on screen: an event
  // very often has no surviving cue at all. They are collected into their own
  // map and submitted alongside the line edits.
  function collectEvents() {
    const known = new Map((scene?.events || []).map(e => [e.event_id, e]));
    container.querySelectorAll('.scene-event').forEach(node => {
      const id = node.dataset.event;
      const before = known.get(id);
      if (!before) return;
      const decision = node.querySelector('.scene-decision').value;
      const asset = node.querySelector('.scene-asset').value.trim();
      if (decision === before.decision && asset === (before.asset || '')) {
        pendingEvents.delete(id);
        return;
      }
      pendingEvents.set(id, { event: id, decision, ...(asset ? { asset } : {}) });
    });
    return [...pendingEvents.values()];
  }

  // A note or a verdict about a reaction is new information about this run, so
  // it goes to the decisions sidecar exactly as a cue verdict does — not into
  // the snapshot, and not through a re-render.
  async function saveCoverage(button) {
    const status = container.querySelector('#sceneCoverageStatus');
    const note = container.querySelector('#sceneCoverageNote').value;
    const nodes = [...container.querySelectorAll('.scene-event')];
    button.disabled = true;
    status.textContent = 'Saving…';
    try {
      for (const node of nodes) {
        const dispositions = [...node.querySelectorAll('.scene-event-finding')]
          .map(row => ({ finding: row.dataset.finding,
            disposition: row.querySelector('.scene-event-disposition').value }));
        if (!dispositions.length && !note) continue;
        await api(`jobs/${getJobId()}/decisions`, { method: 'POST', json: {
          event: node.dataset.event, base_revision: getData()?.revision,
          dispositions, note,
        } });
      }
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
    collectEvents();
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
    const line = getRow() || { index: -1 };
    return { ...patch, ...collectTiming(line), ...collectSpace(line) };
  }

  return {
    load,
    collect,
    events: () => [...pendingEvents.values()],
    stop() { player.stop(); },
    reset() {
      epoch += 1; scene = null; versions = []; context = 2;
      pendingTiming.clear(); pendingEvents.clear(); pendingSpace.clear();
      player.stop(); container.innerHTML = '';
    },
  };
}
