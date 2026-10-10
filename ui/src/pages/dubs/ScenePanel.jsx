import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { api } from '../../lib/api.js';
import { SOURCES } from '../../lib/scene-player.js';
import { formatTime } from '../../lib/media.js';
import { initialForm, spaceShown } from './scene-collect.js';

// The scene panel inside the review editor: hear the exchange, switch between
// the original and the dub at the same position, inspect a finding, choose a
// take, and set a level — all without leaving the line you are reviewing.
//
// Its form fields are uncontrolled and the panel is remounted whenever the line
// or its scene changes; scene-collect.js reads them when the editor collects.

const MODES = [
  ['unknown', 'Not specified'], ['normal', 'Normal speech'], ['thought', 'Inner thought'],
  ['whisper', 'Whisper'], ['shout', 'Shout'], ['call', 'Calling out'],
  ['broadcast', 'Through a speaker'],
];
const DISPOSITIONS = ['open', 'accepted', 'fixed'];
const DECISIONS = ['unresolved', 'retain', 'replace', 'omit', 'covered'];

const seconds = s => (s == null ? '—' : `${Number(s).toFixed(2)}s`);

function windowNote(scene) {
  if (!scene) return '';
  const cues = scene.cues?.length || 0;
  const span = scene.target;
  const source = scene.source ? ` · original ${formatTime(scene.source.start)}–${formatTime(scene.source.end)}` : '';
  return `${cues} line${cues === 1 ? '' : 's'} · ${formatTime(span.start)}–${formatTime(span.end)}`
    + `${source} · window grouped by silence, not a detected scene cut`;
}

function takeNote(take) {
  if (take.state === 'failed') return `failed: ${take.error || 'no reason recorded'}`;
  if (!take.available) return 'audio is no longer on disk';
  const checks = take.checks || {};
  if (checks.state === 'defective') return `checks: ${(checks.defects || []).join(', ')}`;
  if (checks.duration) return `${checks.duration.toFixed(2)}s · ${checks.rms_db} dB`;
  return take.direction ? `directed: ${take.direction}` : 'no technical checks recorded';
}

function intentNote(intent) {
  if (!intent.effective) return 'No delivery direction has been applied to this line.';
  // Which layer contributed each part, so an inherited direction and this
  // line's own override are told apart rather than blurred into one string.
  const layers = (intent.sources || []).map(s => `${s.origin}: ${s.text}`).join(' · ');
  const from = layers ? ` From ${layers}.` : '';
  if (intent.capability === 'supported') return `Applied: ${intent.effective}.${from}`;
  if (intent.capability === 'unsupported') {
    return `Asked for "${intent.effective}" — this engine does not accept delivery `
      + `instructions, so it was not applied.${from}`;
  }
  return `Composed "${intent.effective}" — this engine's capability is unknown.${from}`;
}

function levelNote(level, measurement) {
  if (!level.mode || level.mode === 'legacy') return 'Levels: the pre-timing loudness pass owns this run.';
  const parts = [`Levels: ${level.mode}`];
  if (level.outcome === 'fallback') parts.push(`fell back — ${level.reason}`);
  else if (level.outcome === 'clamped') parts.push(level.reason);
  else if (level.applied_db) parts.push(`${level.applied_db > 0 ? '+' : ''}${level.applied_db} dB`);
  else parts.push('no performance gain needed');
  if (measurement.state && measurement.state !== 'measured') {
    parts.push(`source evidence: ${measurement.state}`);
  } else if (measurement.relative_db != null) {
    parts.push(`original was ${measurement.relative_db > 0 ? '+' : ''}${measurement.relative_db} dB vs ordinary dialogue`);
  }
  if (level.peak_limited) parts.push('held back by the peak ceiling');
  return parts.join(' · ');
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

function AnchorNote({ plan, phraseId }) {
  const anchor = (plan.anchors || []).find(a => a.phrase_id === phraseId && a.kind === 'hard' && a.error != null);
  if (!anchor) return null;
  const off = Math.abs(anchor.error);
  if (off <= (anchor.tolerance ?? 0.12)) return <span className="hint">landed on time</span>;
  return <span className="review-marker">landed {off.toFixed(2)}s {anchor.error > 0 ? 'late' : 'early'}</span>;
}

function pauseNote(pause) {
  const length = pause.clip ? (pause.clip.end - pause.clip.start) : 0;
  const planned = pause.planned == null ? '' : ` to ${pause.planned.toFixed(2)}s`;
  const kind = pause.protected ? 'kept as performance' : 'available as padding';
  return `${length.toFixed(2)}s${planned} · ${kind}`;
}

function collisionLabel(code) {
  return {
    timing_collision: 'Introduced collision',
    timing_self_overlap: 'Talking over themselves',
    timing_overlap_intended: 'Original overlap kept',
    timing_overlap_accepted: 'Accepted as deliberate',
  }[code] || code.replaceAll('_', ' ');
}

function spaceNote(treatment) {
  const origin = { default: 'the run default', scene: 'a scene rule', line: 'a choice made for this line',
    manual: 'a manual choice', none: 'nothing' }[treatment.origin] || treatment.origin;
  if (treatment.outcome === 'applied') {
    const tail = treatment.tail ? `, ringing out for ${treatment.tail.toFixed(2)}s past the words` : '';
    const makeup = treatment.makeup
      ? ` The level was corrected by ${treatment.makeup > 0 ? '+' : ''}${treatment.makeup.toFixed(1)} dB so the effect did not change it.` : '';
    return `Playing through ${treatment.preset}, chosen by ${origin}${tail}.${makeup}`;
  }
  if (treatment.outcome === 'unsupported') {
    return `${treatment.preset} was asked for by ${origin} and this FFmpeg build `
      + `cannot render it (missing ${(treatment.missing || []).join(', ')}), so the line is dry.`;
  }
  if (treatment.outcome === 'bypassed') return treatment.reason || 'No treatment was selected; the line is dry.';
  if (treatment.outcome === 'unavailable' || treatment.outcome === 'failed') {
    return `Asked for ${treatment.preset} and it could not be rendered: ${treatment.reason || 'no reason recorded'}.`;
  }
  return 'No acoustic treatment has been recorded for this line.';
}

function eventNote(event) {
  const state = {
    unresolved: 'Not decided. A subtitle tag proves neither that the sound is missing nor that it survived.',
    retained: 'The original sound is placed here.',
    replaced: 'A supplied sound is placed here.',
    omitted: 'Left out on purpose.',
    covered: 'Already carried by the background.',
    unavailable: 'Asked for, but there is nothing to place.',
    unsupported: 'Asked for, and this engine cannot produce it.',
  }[event.coverage] || 'Not decided.';
  return `${state}${event.reason ? ` ${event.reason}.` : ''}`;
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
    ? ` ${verification.attempts} automatic repair${verification.attempts === 1 ? '' : 's'} were already spent on this line.`
    : '';
  const heard = verification.heard ? ` Heard: “${verification.heard}”.` : '';
  return `Words: ${state}${detail}${heard}${diff ? ` Differences: ${diff}.` : ''}${tries}`;
}

function evidence(finding) {
  const data = { ...finding.evidence };
  delete data.differences;
  const diff = (finding.evidence?.differences || [])
    .map(d => `${d.op} at ${d.at}: expected ${JSON.stringify(d.expected)} heard ${JSON.stringify(d.heard)}`);
  return [JSON.stringify(data, null, 1), ...diff].join('\n');
}

function Takes({ scene, form, update, onPlay }) {
  const rows = scene?.takes || [];
  if (!rows.length) return null;
  return (
    <fieldset className="scene-takes"><legend>Takes</legend>
      {rows.map(t => (
        <label key={t.take_id} className={`scene-take ${t.available ? '' : 'is-missing'}`}>
          <input type="radio" name="sceneTake" value={t.take_id} checked={form.take === t.take_id}
            disabled={!t.available} onChange={() => update({ take: t.take_id })} />
          <span><strong>{t.origin === 'candidate' ? 'Alternative' : 'Original take'}</strong>{' '}
            <span className="m">{t.take_id}</span>{' '}
            <span className="hint">{takeNote(t)}</span></span>
          <button type="button" className="btn btn-ghost scene-audition" disabled={!t.available}
            onClick={() => onPlay('take', { keepPosition: false, takeId: t.take_id })}>Audition</button>
        </label>
      ))}
      <p className="hint">Technical checks explain defects. They are not a judgement about the acting — choosing a take is yours.</p>
    </fieldset>
  );
}

function Direction({ cue, data, form, update }) {
  const intent = cue.intent || {};
  const level = cue.level || {};
  const measurement = cue.measurement || {};
  return (
    <fieldset className="scene-direction"><legend>Performance</legend>
      <label className="review-field">Speech mode
        <select id="sceneMode" className="input" value={form.mode} onChange={e => update({ mode: e.target.value })}>
          {MODES.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
        </select></label>
      <label className="review-field">Delivery traits
        <input id="sceneTraits" className="input" maxLength={200} value={form.traits}
          onChange={e => update({ traits: e.target.value })}
          placeholder="urgent, restrained" /></label>
      <label className="review-field">Line direction
        <input id="sceneDirection" className="input" maxLength={500} value={form.direction}
          onChange={e => update({ direction: e.target.value })}
          placeholder="hold back, almost out of breath" /></label>
      <p className="hint">{intentNote(intent)}</p>
      <div className="review-timing">
        <label className="review-field">Gain (dB)
          <input id="sceneGain" className="input m" type="number" step="0.5" min="-24" max="24"
            value={form.gain} onChange={e => update({ gain: e.target.value })}
            placeholder={String(level.applied_db ?? 0)} /></label>
        <label className="review-field">Alternative takes
          <input id="sceneCandidates" className="input m" type="number" min="0" max={data?.candidate_limit ?? 4}
            value={form.candidates} onChange={e => update({ candidates: e.target.value })} /></label>
      </div>
      <p className="hint">{levelNote(level, measurement)}</p>
    </fieldset>
  );
}

// A reviewer's unit of work here is a phrase and a pause, not a filter graph.
// Every control re-renders the take that already exists; none generates speech.
function Timing({ scene, form, update }) {
  const plan = scene?.phrasing;
  if (!plan || plan.mode === 'unknown') return null;
  if (plan.mode === 'whole') {
    return (
      <fieldset className="scene-timing"><legend>Timing</legend>
        <p className="hint">Whole-clip fitting owns this run: a line that overruns is compressed evenly end to end.
          Set the timing approach to &quot;by phrase&quot; in settings to fit the parts instead.</p>
      </fieldset>
    );
  }
  const inner = (plan.pauses || []).filter(p => p.origin !== 'boundary');
  const rows = scene?.collisions || [];
  return (
    <fieldset className="scene-timing"><legend>Timing</legend>
      <p className="hint">{timingNote(plan)}</p>
      {plan.conflicts?.length > 0 && (
        <ul className="scene-conflicts">{plan.conflicts.map((c, i) => <li key={i}>{c.detail || c.code}</li>)}</ul>
      )}
      <ol className="scene-phrases">
        {(plan.phrases || []).map(p => (
          <li key={p.phrase_id} data-phrase={p.phrase_id}>
            <span className="m">{seconds(p.at)}</span>{' '}
            <span>{p.text || `phrase ${p.order + 1}`}</span>{' '}
            <label className="m">Anchor at{' '}
              <input type="number" className="input m scene-anchor" step="0.05" min="0"
                max={Number(plan.slot ?? 0).toFixed(2)} value={form.anchors[p.phrase_id] ?? ''}
                onChange={e => update({ anchors: { ...form.anchors, [p.phrase_id]: e.target.value } })}
                placeholder={p.at == null ? '' : Number(p.at).toFixed(2)}
                aria-label="Anchor this phrase, seconds after the line starts" /></label>{' '}
            <AnchorNote plan={plan} phraseId={p.phrase_id} />
          </li>
        ))}
      </ol>
      {inner.length > 0 && (
        <div className="scene-pauses"><p className="m">Pauses</p>
          {inner.map(p => (
            <label key={p.pause_id} className="scene-pause">
              <input type="checkbox" className="scene-protect" data-pause={p.pause_id}
                checked={!!form.protect[p.pause_id]}
                onChange={e => update({ protect: { ...form.protect, [p.pause_id]: e.target.checked } })} /> {pauseNote(p)}
            </label>
          ))}
        </div>
      )}
      <label><input type="checkbox" id="sceneBypass" checked={form.bypassTiming}
        onChange={e => update({ bypassTiming: e.target.checked })} /> Leave this line&apos;s timing exactly as generated</label>
      {rows.length > 0 && (
        <div className="scene-collisions">
          {rows.map((r, i) => (
            <p key={i}><strong>{collisionLabel(r.code)}</strong>{' '}
              <span className="m">line {r.with_line} · {r.seconds}s</span>{' '}
              <span className="hint">{r.note || ''}</span>
              {r.accepted?.stale && <span className="review-marker">earlier decision — the audio has changed</span>}</p>
          ))}
          <label><input type="checkbox" id="sceneOverlap" checked={form.overlap}
            onChange={e => update({ overlap: e.target.checked })} /> This overlap is deliberate</label>
        </div>
      )}
      <p className="hint">Changing an anchor or a pause re-renders this line from the take it already has. No new speech is generated.</p>
    </fieldset>
  );
}

// "Where does this line sound like it is", not a filter graph. A preset this
// FFmpeg build cannot render is shown as unavailable rather than offered and
// then refused.
function Space({ scene, data, form, update }) {
  const treatment = scene?.treatment;
  if (!treatment) return null;
  const policy = data?.treatments || {};
  if (!spaceShown(scene, data)) {
    return (
      <fieldset className="scene-space"><legend>Space</legend>
        <p className="hint">Acoustic treatment is off for this run, so every line is placed dry. Turn it on in settings
          to put a room, a distance or a device around a line.</p>
      </fieldset>
    );
  }
  const catalogue = policy.catalogue || [];
  const options = catalogue.length ? catalogue : [{ preset: form.preset, capability: 'unknown' }];
  const findings = scene?.treatment_findings || [];
  return (
    <fieldset className="scene-space"><legend>Space</legend>
      <p className="hint">{spaceNote(treatment)}</p>
      <div className="review-options">
        <label className="review-field">Preset
          <select id="sceneTreatment" className="input" value={form.preset}
            onChange={e => update({ preset: e.target.value })}>
            {options.map(p => (
              <option key={p.preset} value={p.preset} disabled={p.capability === 'unsupported'}>
                {p.preset}{p.capability === 'unsupported' ? ` — unavailable (needs ${(p.missing || []).join(', ')})` : ''}
              </option>
            ))}
          </select></label>
        <label className="review-field">Intensity
          <input id="sceneIntensity" className="input m" type="number" step="0.1" min="0" max="1"
            value={form.intensity} onChange={e => update({ intensity: e.target.value })} /></label>
      </div>
      <label><input type="checkbox" id="sceneNoSpace" checked={form.dry}
        onChange={e => update({ dry: e.target.checked })} /> Leave this line dry whatever the scene says</label>
      {findings.length > 0 && (
        <ul className="scene-conflicts">{findings.map((f, i) => <li key={i}>{f.code.replaceAll('_', ' ')}: {f.note || ''}</li>)}</ul>
      )}
      {catalogue.length > 0 && (
        <details><summary>What each preset does</summary>
          <ul className="hint">{catalogue.map(p => <li key={p.preset}><strong>{p.preset}</strong> — {p.summary || ''}</li>)}</ul>
        </details>
      )}
      <p className="hint">Changing the space re-renders this line from the take it already has. No new speech is generated,
        and the dry line is kept so the choice is reversible.</p>
    </fieldset>
  );
}

// A note or a verdict about a reaction is new information about this run, so
// it goes to the decisions sidecar exactly as a cue verdict does.
function Coverage({ scene, jobId, data, form, update, onPlay, onDecision }) {
  const rows = scene?.events || [];
  const [note, setNote] = useState(rows[0]?.decision?.note || '');
  const [verdicts, setVerdicts] = useState(() => Object.fromEntries(rows.flatMap(e =>
    (e.findings || []).map(f => [`${e.event_id}/${f.finding_id}`, f.disposition]))));
  const [status, setStatus] = useState('');
  const [busy, setBusy] = useState(false);
  const bed = scene?.available?.bed_note || '';
  if (!rows.length) {
    return (
      <fieldset className="scene-coverage"><legend>Coverage</legend>
        <p className="hint">No reaction or background event was recorded in this window. {bed}</p>
      </fieldset>
    );
  }
  async function save() {
    setBusy(true);
    setStatus('Saving…');
    try {
      for (const e of rows) {
        const dispositions = (e.findings || []).map(f => ({
          finding: f.finding_id, disposition: verdicts[`${e.event_id}/${f.finding_id}`] }));
        if (!dispositions.length && !note) continue;
        await api(`jobs/${jobId}/decisions`, { method: 'POST', json: {
          event: e.event_id, base_revision: data?.revision, dispositions, note } });
      }
      setStatus('Saved against this version of the audio.');
      onDecision?.();
    } catch (error) {
      setStatus(error.message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <fieldset className="scene-coverage"><legend>Coverage</legend>
      <p className="hint">{bed}</p>
      {rows.map(e => (
        <div key={e.event_id} className="scene-event" data-event={e.event_id}>
          <p><strong>{e.type}</strong>{' '}
            <span className="m">{seconds(e.target?.start)} · {e.category}</span>{' '}
            <span className="hint">{e.text || ''}</span></p>
          <p className="hint">{eventNote(e)}</p>
          {(e.findings || []).map(f => (
            <div key={f.finding_id} className="scene-event-finding" data-finding={f.finding_id}>
              <p className="review-flags">{f.code.replaceAll('_', ' ')}: {f.evidence?.note || ''}
                {f.review?.stale && <span className="review-marker">earlier verdict — the audio has changed</span>}</p>
              <label className="m">Verdict
                <select className="input scene-event-disposition" value={verdicts[`${e.event_id}/${f.finding_id}`]}
                  onChange={ev => setVerdicts(v => ({ ...v, [`${e.event_id}/${f.finding_id}`]: ev.target.value }))}>
                  {DISPOSITIONS.map(d => <option key={d} value={d}>{d}</option>)}
                </select></label>
            </div>
          ))}
          <div className="review-options">
            <label>Coverage
              <select className="input scene-decision" value={form.events[e.event_id]?.decision ?? e.decision}
                onChange={ev => update({ events: { ...form.events,
                  [e.event_id]: { ...form.events[e.event_id], decision: ev.target.value } } })}>
                {DECISIONS.map(d => <option key={d} value={d}>{d}</option>)}
              </select></label>
            <input className="input scene-asset" maxLength={1000} placeholder="Replacement sound file"
              value={form.events[e.event_id]?.asset ?? ''} onChange={ev => update({ events: { ...form.events,
                [e.event_id]: { ...form.events[e.event_id], asset: ev.target.value } } })} />
            <button type="button" className="btn btn-ghost scene-hear-event" data-event={e.event_id} disabled={!e.playable}
              onClick={() => onPlay('event', { keepPosition: false, eventId: e.event_id })}>Hear it</button>
          </div>
        </div>
      ))}
      <p className="hint">Nothing is inserted without a decision. A coverage change re-mixes; it never generates speech.</p>
      <div className="review-options">
        <input className="input" id="sceneCoverageNote" maxLength={2000}
          placeholder="Note about the reactions or the bed in this scene" value={note}
          onChange={e => setNote(e.target.value)} />
        <button type="button" className="btn btn-secondary" id="sceneSaveCoverage" disabled={busy} onClick={save}>Save coverage notes</button>
      </div>
      <p className="hint" id="sceneCoverageStatus" role="status">{status}</p>
    </fieldset>
  );
}

function Findings({ cue, row, jobId, data, onDecision }) {
  const [status, setStatus] = useState('');
  const [busy, setBusy] = useState(false);
  const rows = (cue.findings || []).filter(f => f.disposition !== 'obsolete');
  const [verdicts, setVerdicts] = useState(() => Object.fromEntries(rows.map(f =>
    [f.finding_id, { disposition: f.disposition, note: f.review?.note || '' }])));
  const setVerdict = (id, patch) => setVerdicts(v => ({ ...v, [id]: { ...v[id], ...patch } }));
  const verification = cue.verification || {};
  const words = verification.state && verification.state !== 'unknown'
    ? <p className="hint">{verificationNote(verification)}</p> : null;
  if (!rows.length) return <div className="scene-findings">{words}</div>;
  async function save() {
    const dispositions = rows.map(f => ({ finding: f.finding_id, ...verdicts[f.finding_id] }));
    setBusy(true);
    setStatus('Saving…');
    try {
      await api(`jobs/${jobId}/decisions`, { method: 'POST', json: {
        cue: row.cue?.cue_id, base_revision: data?.revision, dispositions } });
      setStatus('Saved against this version of the audio.');
      onDecision?.();
    } catch (error) {
      setStatus(error.message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <fieldset className="scene-findings"><legend>Findings</legend>{words}
      {rows.map(f => (
        <div key={f.finding_id} className="scene-finding" data-finding={f.finding_id}>
          <p><strong>{f.code.replaceAll('_', ' ')}</strong>{' '}
            <span className="m">{f.kind} · {f.severity}</span>
            {f.review?.stale && <span className="review-marker">earlier verdict — audio has changed</span>}</p>
          <details><summary>Evidence</summary><pre className="m">{evidence(f)}</pre></details>
          <div className="review-options">
            <label>Verdict
              <select className="input scene-disposition" value={verdicts[f.finding_id].disposition}
                onChange={e => setVerdict(f.finding_id, { disposition: e.target.value })}>
                {DISPOSITIONS.map(d => <option key={d} value={d}>{d}</option>)}
              </select></label>
            <input className="input scene-note" maxLength={2000} placeholder="Why?" value={verdicts[f.finding_id].note}
              onChange={e => setVerdict(f.finding_id, { note: e.target.value })} />
          </div>
        </div>
      ))}
      <button type="button" className="btn btn-secondary" id="sceneSaveVerdicts" disabled={busy} onClick={save}>Save verdicts</button>
      <p className="hint" id="sceneVerdictStatus" role="status">{status}</p>
      <p className="hint">A rendered export never marks a scene reviewed. These verdicts are recorded against this exact version of the audio.</p>
    </fieldset>
  );
}

// Props: row, data (the review), jobId, scene (null while loading), status
// (the player/loader line), kind (the selected source), audio + player (the
// review's one audio element and its controller), state (scene-collect state),
// onPlay(kind, options), onTab(kind), onContext(n), onDecision(), and
// onForm(form, edited): the panel's fields, on mount and on each edit.
export function ScenePanel({ row, data, jobId, scene, status, kind, audio, player, state, onPlay, onTab, onContext, onDecision, onForm }) {
  const cue = row.cue || {};
  const [form, setForm] = useState(() => initialForm({ scene, row, state, data }));
  const update = patch => setForm(f => ({ ...f, ...patch }));
  const mounted = useRef(false);
  useLayoutEffect(() => {
    onForm(form, mounted.current);
    mounted.current = true;
  }, [form, onForm]);
  const available = scene?.available || {};
  const versions = state.versions;
  const contextRef = useRef(null);

  // Commit semantics, like the native change event: one reload per edit.
  useEffect(() => {
    const input = contextRef.current;
    if (!input) return undefined;
    const change = event => {
      const wanted = Math.max(0, Math.min(8, Number(event.target.value) || 0));
      if (wanted !== state.context) onContext(wanted);
    };
    input.addEventListener('change', change);
    return () => input.removeEventListener('change', change);
  }, [onContext, state]);

  const playable = source => (source.kind === 'version' ? versions.length > 0 : available[source.kind] !== false);

  return (
    <div id="reviewScene">
      <div className="scene" role="group" aria-label="Scene playback">
        <div className="scene-tabs" role="tablist" aria-label="What to play">
          {SOURCES.map(s => (
            <button key={s.kind} type="button" role="tab" className="btn btn-ghost scene-tab" data-kind={s.kind}
              aria-selected={kind === s.kind ? 'true' : 'false'} disabled={!playable(s)}
              onClick={() => onTab(s.kind)}>{s.label}</button>
          ))}
        </div>
        {versions.length > 0 && (
          <label className="scene-version">Compare with{' '}
            <select id="sceneVersion" className="input" defaultValue={player.version || versions[0].version_id}
              onChange={e => { player.setVersion(e.target.value); if (player.kind === 'version') onPlay('version'); }}>
              {versions.map(v => (
                <option key={v.version_id} value={v.version_id}>{v.name} · {(v.created_at || '').slice(0, 16).replace('T', ' ')}</option>
              ))}
            </select></label>
        )}
        {/* The review's single audio element, moved here so switching sources never stacks players. */}
        <div className="scene-audio" ref={el => { if (el && audio.parentNode !== el) el.append(audio); }} />
        <div className="scene-controls">
          <label>Context{' '}
            <input ref={contextRef} type="number" id="sceneContext" className="input m" min="0" max="8"
              defaultValue={state.context} aria-label="Neighbouring lines to include" /></label>
          <label><input type="checkbox" id="sceneLoop" defaultChecked={audio.loop}
            onChange={e => player.setLoop(e.target.checked)} /> Loop</label>
          <label><input type="checkbox" id="sceneMatch" disabled={!cue.level?.applied_db}
            onChange={e => player.setMatched(e.target.checked)} /> Match levels (audition only)</label>
          <span className="hint">{windowNote(scene)}</span>
        </div>
        <p className="hint" id="sceneStatus" role="status">{status || ''}</p>
        {available.source === false && (
          <p className="hint">{scene?.available?.note || 'The original audio for this run is not on disk.'}</p>
        )}
      </div>
      <Takes scene={scene} form={form} update={update} onPlay={onPlay} />
      <Direction cue={cue} data={data} form={form} update={update} />
      <Timing scene={scene} form={form} update={update} />
      <Space scene={scene} data={data} form={form} update={update} />
      <Coverage scene={scene} jobId={jobId} data={data} form={form} update={update} onPlay={onPlay}
        onDecision={onDecision} />
      <Findings cue={cue} row={row} jobId={jobId} data={data} onDecision={onDecision} />
    </div>
  );
}
