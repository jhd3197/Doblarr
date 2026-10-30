import { useEffect, useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { api } from '../../lib/api.js';
import { queryClient } from '../../lib/queries.js';
import { Loading, EmptyState } from '../../components/States.jsx';

// Writing experiments: A (original only), B (English dub only, a diagnostic),
// C (original + aligned English). Frozen before judging; judged blind; the
// held-out official dub is revealed only on request, and the reveal is logged.

const DIMENSIONS = [
  ['source_meaning', 'Source meaning'], ['character_consistency', 'Character'],
  ['regional_naturalness', 'Regional naturalness'], ['humor', 'Humor'],
  ['visual_consistency', 'Fits the picture'], ['timing', 'Timing'],
  ['listening_preference', 'Preference'],
];
const NEUTRAL = ['same', 'neither', 'uncertain', 'n/a'];
const CRITERIA = 'C passes this pilot if it has no critical meaning regression and is preferred, or needs fewer corrections, on most excerpts. Added requests and time are reported alongside.';

// Seconds spent between opening a correction and saving it.
function stopwatch() {
  const began = Date.now();
  return () => (Date.now() - began) / 1000;
}

const evaluationQuery = id => ({ queryKey: ['studio', 'evaluation', id], gcTime: 5_000, queryFn: async () => {
  const data = await api(`studio/evaluations/${id}`);
  const holdout = data.evaluation.revealed.holdout ? (await api(`studio/evaluations/${id}/holdout`)).holdout : null;
  return { ...data, holdout };
} });

function JudgingUnit({ unit, ev, holdout, post, competent }) {
  const [critical, setCritical] = useState('');
  const [errors, setErrors] = useState({});
  const [edited, setEdited] = useState('');
  const mine = dim => ev.judgments.find(j => j.unit_id === unit.unit_id && j.dimension === dim);
  async function judge(dim, choice) {
    try {
      await post('judgments', { unit_id: unit.unit_id, dimension: dim, choice: choice || 'uncertain',
        critical: critical ? [critical] : [], source_competent: competent });
      setErrors({ ...errors, [dim]: '' });
    } catch (error) { setErrors({ ...errors, [dim]: error.message }); }
  }
  async function correct(label, line) {
    const elapsed = stopwatch();
    const text = window.prompt('Corrected line (saved as a new revision; the frozen candidate stays):', line.text);
    if (!text) return;
    try {
      await post('revisions', { label, unit_id: unit.unit_id, slot_id: line.slot_id, text,
        editing_seconds: elapsed() }, '', true);
    } catch (error) { setEdited(`${label}:${error.message}`); }
  }
  const labels = Object.keys(unit.candidates);
  return (
    <div className="studio-unit" data-unit={unit.unit_id}>
      {Object.entries(unit.candidates).map(([label, lines]) => (
        <div key={label} className="studio-candidate-text">
          <strong>Version {label}</strong>
          {lines.map(l => <p key={l.slot_id} data-label={label} data-slot={l.slot_id}>{l.text}</p>)}
          <button type="button" className="btn btn-ghost" data-edit={label} onClick={() => lines[0] && correct(label, lines[0])}>
            {edited.startsWith(`${label}:`) ? edited.slice(label.length + 1) : 'Correct'}</button>
        </div>
      ))}
      {holdout && Object.values(holdout).map(h => (
        <div key={h.label} className="studio-candidate-text studio-holdout"><strong>{h.label}</strong>
          <p>{(h.units || {})[unit.unit_id] || '—'}</p></div>
      ))}
      <div className="studio-dims">
        {DIMENSIONS.map(([dim, name]) => (
          <label key={dim} className="review-filter">{name}
            <select className="input" data-dim={dim} defaultValue={mine(dim)?.choice || ''} onChange={e => judge(dim, e.target.value)}>
              <option value="">—</option>
              {[...labels.map(l => [l, `Version ${l}`]), ...NEUTRAL.map(n => [n, n])].map(([v, t]) => <option key={v} value={v}>{t}</option>)}
            </select>
            {errors[dim] && <span className="review-marker">{errors[dim]}</span>}</label>
        ))}
        <label className="review-filter">Critical meaning error in <select className="input" data-critical value={critical}
          onChange={e => setCritical(e.target.value)}>
          <option value="">none</option>{labels.map(l => <option key={l} value={l}>Version {l}</option>)}</select></label>
      </div>
    </div>
  );
}

function Judging({ id }) {
  const { data } = useQuery(evaluationQuery(id));
  const [competent, setCompetent] = useState(false);
  // The revision the next write builds on: the one read, then each write's answer.
  const revision = useRef(null);
  const read = data?.evaluation.revision;
  useEffect(() => { revision.current = read; }, [read]);
  if (!data) return null;
  const { evaluation: ev, summary, holdout } = data;
  // Every write carries the revision it read; a reveal or correction redraws.
  const post = async (path, json, params = '', redraw = false) => {
    const r = await api(`studio/evaluations/${id}/${path}?base_revision=${revision.current ?? ev.revision}${params}`, { method: 'POST', json });
    revision.current = r.evaluation.revision;
    if (redraw) await queryClient.invalidateQueries({ queryKey: evaluationQuery(id).queryKey });
    return r;
  };
  async function reveal(what) {
    if (what === 'holdout' && !window.confirm('Reveal the official dub? Judgments and corrections made after '
      + 'this are recorded as assisted, and the clean results stay as they are.')) return;
    await post(`reveal/${what}`, undefined, '&actor=studio', true);
  }
  return (
    <div className="studio-judging">
      <div className="studio-card-head"><h4>Blind judging</h4>
        {ev.mapping ? <span className="tag tag-neutral">{Object.entries(ev.mapping).map(([k, v]) => `${k} = ${v}`).join(' · ')}</span>
          : <button type="button" className="btn btn-ghost" data-reveal="labels" onClick={() => reveal('labels')}>Reveal which version is which</button>}
        {ev.revealed.holdout ? <span className="tag tag-outline">official dub revealed — later edits are assisted</span>
          : <button type="button" className="btn btn-ghost" data-reveal="holdout" onClick={() => reveal('holdout')}>Reveal the official dub</button>}
        <a className="btn btn-ghost" href={`/api/studio/evaluations/${id}/export`} download={`evaluation-${id}.json`}>Download results</a></div>
      <label className="studio-check"><input type="checkbox" id="jCompetent" checked={competent}
        onChange={e => setCompetent(e.target.checked)} /> I read the source language (needed for a source-meaning verdict)</label>
      {ev.units.map(u => <JudgingUnit key={`${u.unit_id}:${ev.revision}`} unit={u} ev={ev} holdout={holdout} post={post} competent={competent} />)}
      <div className="studio-summary"><h4>What the answers say so far</h4>
        <p>{summary.gate.state} — {summary.gate.reason}</p>
        <p className="hint">{Object.entries(summary.conditions).map(([c, s]) =>
          `${c}: preferred ${Object.values(s.preferred).reduce((a, b) => a + b, 0)}×, ${s.critical} critical, `
          + `${s.corrections} correction(s), ${s.assisted_revisions} assisted`).join(' · ')}</p>
        <p className="hint">{Object.entries(summary.usage).map(([c, u]) => `${c}: ${u.provider_calls} request(s), `
          + `${u.seconds.toFixed(1)}s, cost ${u.cost == null ? 'unknown' : u.cost}`).join(' · ')}</p>
        <p className="hint">{summary.note}{summary.competent_source_review ? '' : ' No source-meaning verdict here came from a reviewer who reads the source.'}</p>
      </div>
    </div>
  );
}

function Experiment({ ctrl, id }) {
  const { data } = useQuery({ queryKey: ['studio', 'experiment', id], queryFn: () => api(`studio/experiments/${id}`), gcTime: 5_000 });
  const [note, setNote] = useState('');
  const [judging, setJudging] = useState('');
  if (!data) return <Loading />;
  const e = data.experiment;
  const evaluation = judging || data.evaluations[0]?.id;

  async function run() {
    try {
      const r = await api(`studio/experiments/${id}/run`, { method: 'POST', json: {} });
      setNote(`Queued ${r.job.id}. Reopen when it finishes; cancelling keeps finished excerpts.`);
    } catch (error) { setNote(error.message); }
  }
  async function evaluate() {
    try {
      const r = await api(`studio/experiments/${id}/evaluations`, { method: 'POST', json: {} });
      setJudging(r.evaluation.id);
    } catch (error) { setNote(error.message); }
  }
  async function speak(variant) {
    try {
      const r = await api(`studio/experiments/${id}/variants/${variant}/render`, { method: 'POST', json: { job_id: ctrl.jobId() } });
      setNote(`Queued ${r.job.id}: ${r.lines} line(s). ${r.note}`);
    } catch (error) { setNote(error.message); }
  }

  return (
    <div className="studio-experiment">
      <h4>{e.name}</h4>
      <p className="hint">Predeclared: {e.decision_criteria}</p>
      <div className="studio-actions">
        <button type="button" className="btn btn-primary" id="exRun" onClick={run}>Write the conditions</button>
        <button type="button" className="btn btn-secondary" id="exEval" onClick={evaluate}>Judge blind</button></div>
      <p className="hint" id="exNote" role="status">{note}</p>
      <div className="table-wrap"><table className="table"><thead><tr><th>Variant</th><th>State</th><th>Requests</th><th>Time</th><th>Inputs</th><th /></tr></thead><tbody>
        {data.variants.length ? data.variants.map(v => (
          <tr key={v.id}><td>{v.condition} · repeat {v.repeat}</td>
            <td>{v.status}{v.error && <div className="review-marker">{v.error}</div>}
              {v.missing_input?.length > 0 && <div className="hint">{v.missing_input.length} unit(s) had no English to write from</div>}</td>
            <td className="m">{v.usage?.provider_calls ?? 0}{v.usage?.cost == null ? ' · cost unknown' : ` · ${v.usage.cost}`}</td>
            <td className="m">{v.usage?.seconds ?? 0}s</td>
            <td><details><summary>Allowed / excluded</summary>
              <ul className="studio-list">
                {(v.manifest?.allowed || []).map(a => <li key={`a${a.label}${a.role}`}>reads {a.label} ({a.role})</li>)}
                {(v.manifest?.excluded || []).map(x => <li key={`x${x.label}`}>never reads {x.label}: {x.reason}</li>)}
                <li>{v.guard_audit} request(s) scanned; {v.memory_rejected || 0} memory candidate(s) dropped</li></ul></details></td>
            <td>{v.frozen && ctrl.jobId() && <button type="button" className="btn btn-ghost" data-render={v.id} onClick={() => speak(v.id)}>Speak it</button>}</td></tr>
        )) : <tr><td colSpan="6" className="hint">Not written yet.</td></tr>}
      </tbody></table></div>
      <details><summary>Limitations</summary><ul className="studio-list">{(e.limitations || []).map(l => <li key={l}>{l}</li>)}</ul></details>
      <div id="exJudge">{evaluation && <Judging key={evaluation} id={evaluation} />}</div>
    </div>
  );
}

function DefineExperiment({ ctrl, onCreated }) {
  const o = ctrl.state.overview;
  const refs = o.references || [];
  const meaning = refs.filter(r => r.roles.includes('meaning'));
  const held = refs.filter(r => r.evaluation_only);
  const [form, setForm] = useState({
    name: '', meaning: meaning[0]?.id || '', adaptation: '', alignment: '', excerpts: '', criteria: CRITERIA,
    scenes: '', locale: ctrl.state.session.direction?.target_locale || 'es-MX', policy: 'reference_suggestions',
    provider: 'voicebox', model: '', repeats: '1', budget: '60',
  });
  const [hold, setHold] = useState(() => held.map(r => r.id));
  const set = key => e => setForm({ ...form, [key]: e.target.value });

  async function create() {
    const excerpts = form.excerpts.split('\n').map(l => l.split(',').map(x => x.trim()))
      .filter(p => p.length >= 3).map(p => ({ excerpt_id: p[0], start: Number(p[1]), end: Number(p[2]), title: p.slice(3).join(', ') }));
    const scenes = Object.fromEntries(form.scenes.split('\n').map(l => l.split(':'))
      .filter(p => p.length > 1).map(p => [p[0].trim(), p.slice(1).join(':').trim()]));
    const adaptation = form.adaptation || null;
    try {
      const made = await api(`studio/sessions/${ctrl.sid}/experiments`, { method: 'POST', json: {
        name: form.name.trim(), meaning: form.meaning, adaptation, alignment: form.alignment || null,
        evaluation: hold, conditions: adaptation ? ['A', 'B', 'C'] : ['A'], excerpts,
        decision_criteria: form.criteria, shared: { scene_notes: scenes }, policy: form.policy,
        target_locale: form.locale.trim(), provider: form.provider.trim(), model: form.model.trim(),
        repeats: Number(form.repeats) || 1, max_requests: Number(form.budget) || 60 } });
      await ctrl.reload();
      onCreated(made.experiment.id);
    } catch (error) { ctrl.status(error.message); }
  }

  return (
    <details className="studio-add"><summary>Define an experiment</summary>
      <div className="studio-inline">
        <label className="review-field">Name <input className="input" id="exName" value={form.name} onChange={set('name')} /></label>
        <label className="review-field">Original <select className="input" id="exMeaning" value={form.meaning} onChange={set('meaning')}>
          {meaning.map(r => <option key={r.id} value={r.id}>{r.label}</option>)}</select></label>
        <label className="review-field">English reference <select className="input" id="exAdapt" value={form.adaptation} onChange={set('adaptation')}>
          <option value="">None (A only)</option>
          {refs.filter(r => r.roles.includes('adaptation')).map(r => <option key={r.id} value={r.id}>{r.label}</option>)}</select></label>
        <label className="review-field">Alignment <select className="input" id="exAlign" value={form.alignment} onChange={set('alignment')}>
          <option value="">None</option>
          {(o.alignments || []).map(a => <option key={a.id} value={a.id}>{a.id} r{a.revision}</option>)}</select></label>
      </div>
      <fieldset className="studio-fieldset"><legend>Held out for evaluation</legend>
        {held.length ? held.map(r => (
          <label key={r.id} className="studio-check"><input type="checkbox" data-hold={r.id} checked={hold.includes(r.id)}
            onChange={e => setHold(e.target.checked ? [...hold, r.id] : hold.filter(x => x !== r.id))} /> {r.label}</label>
        )) : <p className="hint">None. The experiment still works; judge naturalness, timing and fidelity with a qualified reviewer.</p>}
      </fieldset>
      <label className="review-field">Excerpts, one per line: id, start s, end s, title
        <textarea className="input m" id="exExcerpts" rows={4} placeholder="x1, 95, 140, Return to the village"
          value={form.excerpts} onChange={set('excerpts')} /></label>
      <label className="review-field">Decision criteria, written before looking at any result
        <textarea className="input" id="exCriteria" rows={2} value={form.criteria} onChange={set('criteria')} /></label>
      <label className="review-field">Scene notes (what is seen, never what is said), one per line: excerpt id: note
        <textarea className="input" id="exScenes" rows={2} value={form.scenes} onChange={set('scenes')} /></label>
      <div className="studio-inline">
        <label className="review-field">Region <input className="input m" id="exLocale" value={form.locale} onChange={set('locale')} /></label>
        <label className="review-field">Policy for C <select className="input" id="exPolicy" value={form.policy} onChange={set('policy')}>
          <option value="reference_suggestions">Original plus reference suggestions</option>
          <option value="follow_edition">Follow the English edition</option></select></label>
        <label className="review-field">Provider <input className="input m" id="exProvider" value={form.provider} onChange={set('provider')} /></label>
        <label className="review-field">Model <input className="input m" id="exModel" placeholder="provider default" value={form.model} onChange={set('model')} /></label>
        <label className="review-field">Repeats <input className="input m" type="number" min="1" max="3" id="exRepeats" value={form.repeats} onChange={set('repeats')} /></label>
        <label className="review-field">Request budget <input className="input m" type="number" min="1" max="2000" id="exBudget" value={form.budget} onChange={set('budget')} /></label>
      </div>
      <button type="button" className="btn btn-primary" id="exCreate" onClick={create}>Freeze this definition</button>
      <p className="hint">Freezing pins the alignment revision, the allowed inputs and the settings. A changed
        definition is a new experiment.</p>
    </details>
  );
}

export function StudioExperiments({ ctrl }) {
  const list = ctrl.state.overview.experiments || [];
  const [open, setOpen] = useState('');
  return (
    <div id="cmpExperiments">
      <section className="panel studio-card" aria-labelledby="exHead">
        <h3 id="exHead">Writing experiments</h3>
        <p className="hint">Does an aligned English dub help the Spanish? A writes from the original only, B from
          the English dub only (a diagnostic), C from both. Region, scene notes, model and budget are held
          the same. Improvement is a question here, not an assumption.</p>
        <div id="exList">{list.length ? list.map(e => (
          <div key={e.id} className="studio-align-row"><strong>{e.name}</strong>
            <span className="hint">{e.conditions.join('/')} · {e.excerpts} excerpt(s) · {e.target_locale}
              {' '}· {e.provider}{e.model ? `/${e.model}` : ''}</span>
            <button type="button" className="btn btn-ghost" data-exp={e.id} onClick={() => setOpen(e.id)}>Open</button></div>
        )) : <EmptyState>No experiment yet.</EmptyState>}</div>
        <DefineExperiment ctrl={ctrl} onCreated={setOpen} />
        <div id="exDetail">{open && <Experiment key={open} ctrl={ctrl} id={open} />}</div>
      </section>
    </div>
  );
}
