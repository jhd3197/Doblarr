import { useLayoutEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { api } from '../../lib/api.js';
import { KnowledgeCorrection } from '../../components/KnowledgeCorrection.jsx';
import { EnvelopePanel } from './EnvelopePanel.jsx';
import { formatTime } from '../../lib/media.js';


// What this line actually is and what it was rendered from. Unknown stays
// visible as unknown; it is not the same as "nothing to report".
function provenance(row) {
  const cue = row.cue || {};
  const source = (cue.source?.spans || []).map(s => `${formatTime(s.start)}–${formatTime(s.end)}`).join(', ') || 'unrecorded';
  const takes = cue.audio?.takes?.length || 0;
  const rendered = (cue.audio?.renders || []).slice(-1)[0];
  const role = rendered ? rendered.role : (cue.audio?.takes?.length ? 'raw' : 'none');
  const proven = rendered && rendered.proven === false ? ' (unverified role)' : '';
  return `Cue ${cue.cue_id || 'unassigned'} · source ${source} · `
    + `${takes} take${takes === 1 ? '' : 's'} · rendered from ${role}${proven}`;
}

// What boundary preparation decided, and why. "kept" and "uncertain" are not
// failures — leaving a take alone is the safe answer — so both say so plainly.
function preparation(row) {
  const p = row.cue?.preparation;
  if (!p || !p.decision || p.decision === 'unknown') return 'Boundaries: not analyzed.';
  const parts = [];
  if (p.decision === 'trimmed') parts.push(`trimmed ${(p.lead ?? 0).toFixed(2)}s lead / ${(p.tail ?? 0).toFixed(2)}s tail`);
  else parts.push(`${p.decision}${p.reason ? ` — ${p.reason}` : ''}`);
  if (p.active_duration) parts.push(`${p.active_duration.toFixed(2)}s of speech`);
  if (p.onset) parts.push(`speaks ${p.onset.toFixed(2)}s after the cue starts`);
  if (p.silences?.length) parts.push(`${p.silences.length} internal pause${p.silences.length === 1 ? '' : 's'} kept`);
  if ((row.cue?.audio?.renders || []).some(r => r.role === 'edged')) parts.push('edges faded');
  return `Boundaries: ${parts.join(' · ')}`;
}

// Private complete-line memory: only reviews the reviewer actually performed.
function MemoryForm({ row, data, values }) {
  const [form, setForm] = useState({ reviewer: '', meaning: false, natural: false, timing: false });
  const [status, setStatus] = useState('');
  const [busy, setBusy] = useState(false);
  const check = key => e => setForm(f => ({ ...f, [key]: e.target.checked }));
  async function save() {
    setBusy(true);
    try {
      await api('memory', { method: 'POST', json: {
        source_lang: data.source_language || 'und', target_locale: data.locale || data.language,
        source_text: row.text_src, target_text: values.text,
        context: row.memory_context || {}, duration: Number(values.end) - Number(values.start),
        reviewer: form.reviewer,
        meaning_reviewed: form.meaning, naturalness_reviewed: form.natural, timing_reviewed: form.timing,
        status: form.meaning && form.natural && form.timing ? 'reviewed' : 'proposed',
      } });
      setStatus(row.memory_context?.register
        ? 'Saved privately. New jobs can reuse it when all conditions match and reuse is enabled.'
        : 'Saved privately as guidance. This line has no recorded speaker register, so automatic reuse is unavailable.');
    } catch (e) { setStatus(e.message); setBusy(false); }
  }
  return (
    <>
      <p className="hint">Private complete-line memory. Matching scene, speaker register, settings and timing are required.
        Record only reviews you have performed.</p>
      <label className="review-field">Reviewer
        <input className="input memory-reviewer" value={form.reviewer} onChange={e => setForm(f => ({ ...f, reviewer: e.target.value }))} /></label>
      <label><input type="checkbox" className="memory-meaning" checked={form.meaning} onChange={check('meaning')} /> Source meaning verified</label>
      <label><input type="checkbox" className="memory-natural" checked={form.natural} onChange={check('natural')} /> Regional wording verified</label>
      <label><input type="checkbox" className="memory-timing" checked={form.timing} onChange={check('timing')} /> Timing verified by listening</label>
      <button type="button" className="btn btn-secondary memory-save" disabled={busy} onClick={save}>Save privately</button>
      <p className="memory-status hint" role="status">{status}</p>
    </>
  );
}

// After a correction is saved: which lines it touches here, and a re-render of
// just those with the updated knowledge.
function CorrectionSaved({ entry, data, jobId, onQueued, onLocked }) {
  const affected = data.segments
    .filter(s => entry.phrase && (s.text_translated || s.text_src).includes(entry.phrase)).map(s => s.index);
  const [state, setState] = useState({ queued: false, error: '', busy: false });
  async function rerender() {
    setState({ queued: false, error: '', busy: true });
    try {
      await api(`jobs/${jobId}/review`, { method: 'POST', json: {
        edits: affected.map(index => ({ index, regenerate: true })),
        use_updated_knowledge: true, base_revision: data.revision,
      } });
      onLocked();
      setState({ queued: true, error: '', busy: false });
      onQueued();
    } catch (error) { setState({ queued: false, error: error.message, busy: false }); }
  }
  if (state.queued) return <p className="hint">Queued with the updated knowledge. Unchanged clips will be reused.</p>;
  return (
    <>
      <p className="hint">{state.error || <>Correction saved for {entry.locale} ({entry.scope} scope).{' '}
        {affected.length} line{affected.length === 1 ? '' : 's'} use “{entry.phrase}” here.</>}</p>
      <button type="button" className="btn btn-secondary" id="reviewRerender"
        disabled={state.busy || !(affected.length && data.editable)} onClick={rerender}>Re-render affected lines with the correction</button>
    </>
  );
}

// The editor for one line, remounted per line. Its fields are React state;
// `onValues(values, edited)` hands them to the review: on mount, and on each edit.
// `scene` is the scene panel element, rendered by the review.
export function LineEditor({ row, data, jobId, edit, voices, onValues, scene, onQueued, onLocked }) {
  const [box, setBox] = useState(null);          // null | { kind: 'saved', entry } | { kind: 'memory' }
  const [correction, setCorrection] = useState(null);
  const voice = edit.voice ?? row.voice ?? '';
  const [values, setValues] = useState(() => ({
    text: edit.text ?? row.text_translated ?? row.text_src, start: String(edit.start ?? row.start),
    end: String(edit.end ?? row.end), voice, delivery: edit.delivery ?? row.delivery ?? '',
    regenerate: !!edit.regenerate, exclude: !!edit.exclude }));
  const mounted = useRef(false);
  useLayoutEffect(() => {
    onValues(values, mounted.current);
    mounted.current = true;
  }, [values, onValues]);
  const change = key => e => {
    const value = e.target.type === 'checkbox' ? e.target.checked : e.target.value;
    setValues(current => ({ ...current, [key]: value }));
  };
  const choices = [...voices];
  if (voice && !choices.some(v => v.id === voice)) choices.push({ id: voice, name: voice });
  const disabled = !data.editable;
  const lineRef = data.title_ref ? `${data.title_ref}#${row.index}` : '';

  return (
    <>
      <div className="review-line-heading"><h3>Line {row.index + 1}</h3><span className="m">{row.speaker}</span></div>
      <p className="review-source">{row.text_src}</p>
      {!row.has_audio && <p className="hint">No generated audio yet.</p>}
      {row.issues.length > 0 && <p className="review-flags">{row.issues.map(i => i.replaceAll('_', ' ')).join(' · ')}</p>}
      <label className="review-field">Dubbed dialogue
        <textarea id="reviewText" className="input" rows={4} disabled={disabled}
          value={values.text} onChange={change('text')} /></label>
      <div className="review-timing">
        <label className="review-field">Start (seconds)
          <input id="reviewStart" className="input m" type="number" min="0" step="0.01" disabled={disabled} value={values.start} onChange={change('start')} /></label>
        <label className="review-field">End (seconds)
          <input id="reviewEnd" className="input m" type="number" min="0" step="0.01" disabled={disabled} value={values.end} onChange={change('end')} /></label>
      </div>
      <label className="review-field">Voice
        <select id="reviewVoice" className="input" disabled={disabled} value={values.voice} onChange={change('voice')}>
          <option value="">Use character voice</option>
          {choices.map(v => <option key={v.id} value={v.id}>{v.name}</option>)}
        </select></label>
      <label className="review-field">Delivery
        <input id="reviewDelivery" className="input" maxLength={500} disabled={disabled}
          value={values.delivery} onChange={change('delivery')} placeholder="For example: speak quietly" /></label>
      <p className="hint">Delivery instructions require a Qwen voice engine.</p>
      <div className="review-options">
        <label><input id="reviewRegenerate" type="checkbox" disabled={disabled} checked={values.regenerate} onChange={change('regenerate')} /> Generate a new take</label>
        <label><input id="reviewExclude" type="checkbox" disabled={disabled} checked={values.exclude} onChange={change('exclude')} /> Exclude this line</label>
      </div>
      <div className="review-options">
        <button type="button" className="btn btn-ghost" id="reviewFixPron" onClick={() => setCorrection('pronunciation')}>Fix pronunciation</button>
        <button type="button" className="btn btn-ghost" id="reviewFixTerm" onClick={() => setCorrection('term')}>Improve regional wording</button>
        <button type="button" className="btn btn-ghost" id="reviewSaveMemory" onClick={() => setBox({ kind: 'memory' })}>Save translation for reuse</button>
      </div>
      <p className="hint">Translation: {row.translation_provenance?.method || 'legacy'} · {row.translation_provenance?.reason || 'No recorded provenance'}</p>
      <p className="hint">{provenance(row)}</p>
      <p className="hint">{preparation(row)}</p>
      {scene}
      <EnvelopePanel jobId={jobId} row={row} />
      <div id="reviewCorrection" className="review-correction">
        {box?.kind === 'memory' && <MemoryForm row={row} data={data} values={values} />}
        {box?.kind === 'saved' && <CorrectionSaved key={box.entry.id} entry={box.entry} data={data} jobId={jobId}
          onQueued={onQueued} onLocked={onLocked} />}
      </div>
      {correction && createPortal(
        <KnowledgeCorrection kind={correction} locale={data.locale || data.language || 'es'}
          text={row.text_translated || row.text_src} voice={row.profile || row.voice || ''}
          titleRef={data.title_ref || ''} showRef={data.show_ref || ''} lineRef={lineRef}
          onSaved={entry => setBox({ kind: 'saved', entry })} onClose={() => setCorrection(null)} />,
        document.getElementById('app') || document.body)}
    </>
  );
}
