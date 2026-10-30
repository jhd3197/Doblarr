import { useEffect, useRef, useState } from 'react';
import { api, apiUrl, withKey } from '../../lib/api.js';
import { Loading } from '../../components/States.jsx';

// One line's voice envelope in review: what was chosen and why, what the
// render actually did, the other candidates, and the same take heard four
// ways. Changing the envelope queues a rerender that reuses the take (no new
// speech); the panel says so before anything runs.

const cache = new Map();          // job id -> recommendations
let player = null;                // one envelope audition at a time, across lines

const ORIGIN = { manual: 'chosen by hand', judge: 'chosen by the judge', retrieval: 'retrieval (rule)',
  default: 'fallback', none: 'not chosen' };
const OUTCOME = { applied: 'rendered', preserved: 'kept as generated', bypassed: 'suggested, not rendered',
  unsupported: 'not supported here', unavailable: 'not available', unknown: 'not rendered yet' };
const PROBLEMS = [['envelope', 'Envelope'], ['level', 'Level'], ['acting', 'Acting'], ['timing', 'Timing'],
  ['background', 'Background'], ['identity', 'Wrong speaker'], ['source_evidence', 'Bad source evidence']];
const HEAR = [['dry', 'Take as generated'], ['shaped', 'With envelope'],
  ['shaped-matched', 'With envelope, level-matched', 'The envelope at the same overall level as the take, so only the shape differs'],
  ['source', 'Original'], ['mixed', 'In the mix']];

function audioUrl(jobId, cue, kind, matched = false) {
  return withKey(apiUrl(`adaptive/audio?job_id=${encodeURIComponent(jobId)}&cue=${encodeURIComponent(cue)}`
    + `&kind=${kind}${matched ? '&level_matched=true' : ''}`));
}

export function forgetEnvelopes(jobId) { cache.delete(jobId); }

function appliedNote(env) {
  if (env.outcome !== 'applied') return env.reason || '';
  return `Applied at strength ${(env.applied ?? 0).toFixed(2)} of ${(env.requested ?? 0).toFixed(2)} asked`
    + `${env.preserved > 0.05 ? `; ${Math.round(env.preserved * 100)}% of the shape was already in the take` : ''}`
    + ` · moves ${(env.range_db ?? 0).toFixed(1)} dB inside the line · peak ${(env.peak ?? 0).toFixed(2)}`;
}

export function EnvelopePanel({ jobId, row }) {
  const cue = row?.cue?.cue_id;
  const [state, setState] = useState({ loading: true, error: '', data: null, templates: [] });
  const [status, setStatus] = useState('');
  const [promote, setPromote] = useState(null);   // feedback id a "good" verdict can be promoted from
  const alive = useRef(true);
  useEffect(() => () => { alive.current = false; }, []);

  useEffect(() => {
    if (!jobId || !cue) return;
    (async () => {
      let data = cache.get(jobId);
      if (!data) {
        try { data = await api(`adaptive/recommendations?job_id=${encodeURIComponent(jobId)}`); }
        catch (error) { if (alive.current) setState(s => ({ ...s, loading: false, error: error.message })); return; }
        cache.set(jobId, data);
      }
      let templates = [];
      try { templates = (await api('templates?kind=voice')).templates; } catch { /* list stays empty */ }
      if (alive.current) setState({ loading: false, error: '', data, templates });
    })();
  }, [jobId, cue]);

  const line = state.data?.lines.find(l => l.cue === cue);
  const env = line?.envelope || {};
  const chosen = env.template?.id || '';
  const [template, setTemplate] = useState(null);
  const [strength, setStrength] = useState(null);
  const [problem, setProblem] = useState(PROBLEMS[0][0]);

  if (!jobId || !cue) return <div id="reviewEnvelope" />;
  if (state.loading) return <div id="reviewEnvelope"><Loading>Reading the envelope recommendations…</Loading></div>;
  if (state.error) return <div id="reviewEnvelope"><p className="hint">{state.error}</p></div>;
  if (!line) return <div id="reviewEnvelope" />;

  const data = state.data;
  const pickedTemplate = template ?? (chosen || 'voice/preserve');
  const pickedStrength = strength ?? (env.requested || 1).toFixed(2);

  function hear(kind) {
    player?.pause();
    player = new Audio(audioUrl(jobId, cue, kind.startsWith('shaped') ? 'shaped' : kind, kind === 'shaped-matched'));
    player.play().catch(error => setStatus(`Could not play: ${error.message}`));
    setStatus(kind === 'shaped-matched' ? 'Level-matched: the loudness difference is removed on purpose.' : '');
    setPromote(null);
  }

  async function select(body) {
    try {
      const result = await api('adaptive/select', { method: 'POST', json: { job_id: jobId, cue, ...body } });
      forgetEnvelopes(jobId);
      setStatus(`Queued (${result.note}) The new render appears as a new run.`);
    } catch (error) { setStatus(error.message); }
    setPromote(null);
  }

  async function verdict(kind) {
    setPromote(null);
    try {
      const saved = await api('adaptive/feedback', { method: 'POST', json: {
        job_id: jobId, cue, verdict: kind, template: env.template || {}, params: env.params || {},
        artifact: env.inputs || '', decision: env.decision || '',
        problems: kind === 'reject' ? [problem] : [] } });
      if (kind !== 'accept') { setStatus('Saved. It stays out of future recommendations.'); return; }
      setStatus('Saved. ');
      setPromote(saved.id);
    } catch (error) { setStatus(error.message); }
  }

  async function promoteExample() {
    try {
      await api('adaptive/examples', { method: 'POST', json: { feedback_id: promote, job_id: jobId } });
      setStatus('Added. Future recommendations can learn from this line.');
    } catch (error) { setStatus(error.message); }
    setPromote(null);
  }

  const mode = data.mode === 'off' ? 'Envelopes are off for this run; a choice made here still applies.'
    : `Mode ${data.mode} · decided by ${data.judge || 'retrieval'}${data.judge_is_model ? '' : ' (a rule, not a model)'}`;

  return (
    <div id="reviewEnvelope">
      <details className="envelope-panel" open>
        <summary><strong>Voice envelope</strong>{' '}
          <span className="hint">{chosen || 'none'} · {ORIGIN[env.origin] || 'not chosen'} · {OUTCOME[env.outcome] || ''}</span></summary>
        <p className="hint">{mode}</p>
        {env.template?.id && <p className="hint">{appliedNote(env)}</p>}
        {line.warnings?.length > 0 && <p className="hint">{line.warnings.join(' ')}</p>}
        <div className="envelope-listen">
          {HEAR.map(([kind, label, title]) => (
            <button key={kind} type="button" className="btn btn-ghost" data-hear={kind} title={title} onClick={() => hear(kind)}>{label}</button>
          ))}
        </div>
        {line.candidates?.length ? (
          <div className="table-wrap"><table className="table envelope-candidates">
            <thead><tr><th>Template</th><th>Score</th><th>Why</th></tr></thead>
            <tbody>
              {line.candidates.map(c => (
                <tr key={c.template.id}>
                  <td>{c.title}{c.template.id === chosen && <> <span className="tag tag-neutral">chosen</span></>}</td>
                  <td className="m">{c.score.toFixed(2)}</td>
                  <td className="hint">{[...c.support, ...c.conflicts.map(x => `but ${x}`)].join('; ') || '—'}</td>
                </tr>
              ))}
            </tbody>
          </table></div>
        ) : <p className="hint">No recommendation was made for this line.</p>}
        <div className="envelope-choose">
          <label className="review-field">Template
            <select className="input" data-env-template value={pickedTemplate} onChange={e => setTemplate(e.target.value)}>
              {state.templates.map(t => <option key={t.id} value={t.id}>{t.title}</option>)}
            </select></label>
          <label className="review-field">Strength{' '}
            <input className="input m" type="number" min="0" max="1.5" step="0.05" data-env-strength
              value={pickedStrength} onChange={e => setStrength(e.target.value)} /></label>
          <button type="button" className="btn btn-secondary" data-env-apply
            onClick={() => select({ template: pickedTemplate, strength: Number(pickedStrength) })}>Render this envelope</button>
          <button type="button" className="btn btn-ghost" data-env-clear onClick={() => select({ clear: true })}>Keep as generated</button>
          {line.locked && <button type="button" className="btn btn-ghost" data-env-undo onClick={() => select({ undo: true })}>Undo my choice</button>}
        </div>
        <p className="hint">Changing the envelope reprocesses the existing take and remixes. No new speech is generated.</p>
        <div className="envelope-verdict">
          <span className="hint">How does it sound?</span>
          <button type="button" className="btn btn-ghost" data-verdict="accept" onClick={() => verdict('accept')}>Good</button>
          <button type="button" className="btn btn-ghost" data-verdict="reject" onClick={() => verdict('reject')}>Not right</button>
          <select className="input" data-problem aria-label="What is wrong" value={problem} onChange={e => setProblem(e.target.value)}>
            {PROBLEMS.map(([k, v]) => <option key={k} value={k}>{v}</option>)}
          </select>
        </div>
        <p className="hint" role="status" data-env-status>{status}
          {promote && <button type="button" className="btn btn-ghost" onClick={promoteExample}>Use as an example for similar lines</button>}</p>
      </details>
    </div>
  );
}
