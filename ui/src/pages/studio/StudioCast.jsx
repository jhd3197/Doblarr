import { useEffect, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { api, apiUrl, withKey } from '../../lib/api.js';
import { queryClient } from '../../lib/queries.js';
import { castingQuery, studioVoicesQuery } from './queries.js';
import { Loading } from '../../components/States.jsx';

// Cast: who speaks with which voice, where that choice came from, and
// auditions that compare voices on the same fixed lines.

const KINDS = [
  ['current', 'Current voice', 'the takes this run already has; nothing is generated'],
  ['preset', 'Preset voice', 'a voice from the engine\'s catalogue'],
  ['directed', 'Directed preset', 'a preset plus a delivery instruction (only engines that accept one)'],
  ['clone_character', 'Clone the original actor', 'one clean line of this character\'s original performance'],
  ['clone_line', 'Clone each line (experimental)', 'unstable on short lines; not castable'],
];
const PICKS = [
  ['', 'Cleanest line', 'the default: the cleanest solo line near 8 s'],
  ['lively', 'Liveliest line', 'the line whose pitch moves most; for excitable characters'],
  ['low', 'Lowest line', 'the lowest-pitched line; for a young male voice that reads too high'],
  ['high', 'Brightest line', 'the highest-pitched line; for a deep voice that reads too low'],
];

// One audition take at a time across the whole cast view.
let playing = null;
function playTake(url, onError) {
  playing?.pause();
  playing = new Audio(url);
  playing.play().catch(onError);
}
function stopTake() {
  playing?.pause();
  playing = null;
}

// "+2 st pitch · +4 st formant", or '' when the voice is unshaped.
function shapeLabel(c) {
  const part = (v, what) => (Number(v) ? `${v > 0 ? '+' : ''}${Number(v)} st ${what}` : '');
  return [part(c.pitch_semitones, 'pitch'), part(c.formant_semitones, 'formant')].filter(Boolean).join(' · ');
}
const shortRef = ref => String(ref).split(/[\\/]/).pop();

function blankCandidate(n) {
  return { name: ['current', 'preset', 'clone'][n] || `c${n}`, kind: ['current', 'preset', 'clone_character'][n] || 'preset',
    engine: n === 2 ? 'chatterbox' : '', voice: '', direction: '', pick: '', pitch_semitones: '0', formant_semitones: '0' };
}

function CandidateRow({ value, voices, onChange }) {
  const set = key => e => onChange({ ...value, [key]: e.target.value });
  return (
    <div className="studio-candidate">
      <label className="review-field">Name <input className="input m" data-f="name" value={value.name} onChange={set('name')} /></label>
      <label className="review-field">Kind <select className="input" data-f="kind" value={value.kind} onChange={set('kind')}>
        {KINDS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}</select></label>
      <label className="review-field">Engine <input className="input m" data-f="engine" placeholder="chatterbox, qwen_custom_voice…"
        value={value.engine} onChange={set('engine')} /></label>
      <label className="review-field">Voice <select className="input" data-f="voice" value={value.voice} onChange={set('voice')}>
        <option value="">—</option>{voices.map(v => <option key={v.id} value={v.id}>{v.name}</option>)}</select></label>
      <label className="review-field">Direction <input className="input" data-f="direction" placeholder="only for directed presets"
        value={value.direction} onChange={set('direction')} /></label>
      <label className="review-field">Clone from <select className="input" data-f="pick" title="Which line of the original actor a clone learns from"
        value={value.pick} onChange={set('pick')}>{PICKS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}</select></label>
      <label className="review-field">Pitch <input className="input m" type="number" step="0.5" min="-6" max="6" data-f="pitch_semitones"
        title="Semitones, applied after generation" value={value.pitch_semitones} onChange={set('pitch_semitones')} /></label>
      <label className="review-field">Formant <input className="input m" type="number" step="0.5" min="-6" max="6" data-f="formant_semitones"
        title="Semitones; moves how big the voice sounds, which is most of what makes it read male or female"
        value={value.formant_semitones} onChange={set('formant_semitones')} /></label>
    </div>
  );
}

function AuditionForm({ ctrl, cast, voices }) {
  const [character, setCharacter] = useState(cast.speakers[0]?.speaker || '');
  const [per, setPer] = useState('1');
  const [retakes, setRetakes] = useState('0');
  const [budget, setBudget] = useState('40');
  const [check, setCheck] = useState(false);
  const [rows, setRows] = useState(() => [0, 1, 2].map(blankCandidate));

  async function create() {
    const candidates = rows.map(r => {
      const clone = r.kind === 'clone_character' || r.kind === 'clone_line';
      return { name: r.name.trim(), kind: r.kind, engine: r.engine.trim(), voice: r.voice,
        direction: r.direction.trim(), pick: clone ? r.pick : '',
        pitch_semitones: r.kind === 'current' ? 0 : Number(r.pitch_semitones) || 0,
        formant_semitones: r.kind === 'current' ? 0 : Number(r.formant_semitones) || 0 };
    }).filter(c => c.name);
    try {
      await api(`studio/sessions/${ctrl.sid}/auditions`, { method: 'POST', json: {
        character, job_id: ctrl.jobId(), per_category: Number(per) || 1, retakes: Number(retakes) || 0,
        max_requests: Number(budget) || 40, check_words: check, candidates } });
      await ctrl.refresh();
    } catch (error) { ctrl.status(error.message); }
  }

  return (
    <>
      <p className="hint">Fixed lines from this run, chosen by what the original actor did — calm, quiet,
        intense, in an exchange. Every candidate says the same lines through the same processing.</p>
      <div className="studio-inline">
        <label className="review-field">Character <select className="input" id="auChar" value={character} onChange={e => setCharacter(e.target.value)}>
          {cast.speakers.map(r => <option key={r.speaker}>{r.speaker}</option>)}</select></label>
        <label className="review-field">Lines per type <input className="input m" type="number" min="1" max="3" id="auPer" value={per} onChange={e => setPer(e.target.value)} /></label>
        <label className="review-field">Retakes if flagged <input className="input m" type="number" min="0" max="2" id="auRetakes" value={retakes} onChange={e => setRetakes(e.target.value)} /></label>
        <label className="review-field">Request budget <input className="input m" type="number" min="1" max="400" id="auBudget" value={budget} onChange={e => setBudget(e.target.value)} /></label>
        <label className="studio-check"><input type="checkbox" id="auCheck" checked={check} onChange={e => setCheck(e.target.checked)} /> Check words by recognition</label>
      </div>
      <div id="auCandidates">
        {rows.map((r, i) => <CandidateRow key={i} value={r} voices={voices}
          onChange={next => setRows(rows.map((x, j) => (j === i ? next : x)))} />)}
        <p className="hint">{KINDS.map(([v, l, note], i) => <span key={v}>{i > 0 && '. '}<strong>{l}</strong>: {note}</span>)}.</p>
        <p className="hint"><strong>Clone from</strong> only applies to clones: {PICKS.slice(1).map(([, l, note]) => `${l.toLowerCase()}, ${note}`).join('; ')}.
          {' '}<strong>Pitch</strong> and <strong>formant</strong> shape every take after it is generated; a voice that reads as the
          wrong gender usually needs formant more than pitch. Whatever you cast keeps its shaping.</p>
      </div>
      <div className="studio-actions">
        <button type="button" className="btn btn-ghost" id="auAdd" onClick={() => rows.length < 6 && setRows([...rows, blankCandidate(rows.length)])}>Add a candidate</button>
        <button type="button" className="btn btn-primary" id="auCreate" onClick={create}>Plan the audition</button>
      </div>
    </>
  );
}

function Audition({ ctrl, id, character }) {
  const query = { queryKey: ['studio', ctrl.sid, 'audition', id], queryFn: () => api(`studio/auditions/${id}`), gcTime: 5_000 };
  const { data, error } = useQuery(query);
  const [note, setNote] = useState('');
  if (error) return <section className="panel studio-card studio-audition" data-audition={id}>{error.message}</section>;
  if (!data) return <section className="panel studio-card studio-audition" data-audition={id}><Loading>Loading audition {character}…</Loading></section>;
  const a = data.audition;
  const plan = a.plan || { excerpts: [], missing: [], notes: [] };
  const runnable = !['running'].includes(a.status);

  async function run() {
    try {
      const r = await api(`studio/auditions/${id}/run`, { method: 'POST' });
      setNote(`Queued ${r.job.id}. Takes appear as they finish; cancelling keeps what is done.`);
    } catch (err) { setNote(err.message); }
  }
  function play(name, cue) {
    playTake(withKey(apiUrl(`studio/auditions/${id}/takes/${name}/${cue}`)),
      err => setNote(err.message));
  }
  async function pick(candidate, scope, apply) {
    try {
      const r = await api(`studio/auditions/${id}/select`, { method: 'POST', json: { candidate, scope, apply } });
      setNote(r.rerender ? r.rerender.note : 'Casting saved. Nothing was re-rendered.');
      await ctrl.reload();
      queryClient.invalidateQueries({ queryKey: castingQuery(ctrl.sid).queryKey });
    } catch (err) { setNote(err.message); }
  }
  const castable = a.candidates.filter(c => ['preset', 'directed', 'clone_character'].includes(c.kind) && c.status === 'generated');

  return (
    <section className="panel studio-card studio-audition" data-audition={id}>
      <div className="studio-card-head"><h3>Audition: {a.character}</h3>
        <span className="tag tag-neutral">{a.status}</span>
        {a.usage?.budget && <span className="hint">{a.usage.budget.spent} request(s) spent{a.usage.budget.limit ? ` of ${a.usage.budget.limit}` : ''}</span>}
        <button type="button" className="btn btn-secondary" data-run disabled={!runnable} onClick={run}>
          {a.status === 'planned' ? 'Generate takes' : 'Continue / retry missing takes'}</button></div>
      {a.error && <p className="review-marker">{a.error}</p>}
      {plan.notes.map(n => <p key={n} className="hint">{n}</p>)}
      <div className="studio-audition-grid" role="table" aria-label="Candidates by line">
        <div role="row" className="studio-audition-row"><span role="columnheader">Line</span>
          {a.candidates.map(c => {
            const ref = c.reference_findings;
            return (
              <span key={c.name} role="columnheader"><strong>{c.name}</strong>
                <span className="hint">{c.kind.replace('_', ' ')}{c.engine ? ` · ${c.engine}` : ''}</span>
                {shapeLabel(c) && <span className="tag tag-neutral">{shapeLabel(c)}</span>}
                {ref?.line != null && <span className="hint">from line {ref.line + 1}
                  {c.pick ? ` (${((PICKS.find(p => p[0] === c.pick) || [])[1] || c.pick).toLowerCase()})` : ''}
                  {ref.pitch_hz ? ` · ${Math.round(ref.pitch_hz)} Hz` : ''}
                  {ref.liveliness_st ? ` · moves ${ref.liveliness_st} st` : ''}: “{ref.text || ''}”</span>}
                {c.status && c.status !== 'generated' && <span className="review-marker">{c.status}{c.reason ? `: ${c.reason}` : ''}</span>}
                {(ref?.findings || []).map(f => <span key={f.code} className="hint">{f.code.replace('reference_', '')}: {f.detail}</span>)}
              </span>
            );
          })}
        </div>
        {plan.excerpts.map(x => (
          <div key={x.cue_id} role="row" className="studio-audition-row">
            <span role="cell"><span className="tag tag-neutral">{x.category}</span> {x.text}
              <span className="hint">line {x.index + 1}{x.relative_db != null ? ` · original ${x.relative_db > 0 ? '+' : ''}${x.relative_db} dB` : ''}</span></span>
            {a.candidates.map(c => {
              const take = (a.takes[c.name] || {})[x.cue_id];
              return (
                <span key={c.name} role="cell">{take?.available
                  ? <><button type="button" className="btn btn-ghost" data-play={c.name} data-cue={x.cue_id} onClick={() => play(c.name, x.cue_id)}>Play</button>
                    {take.clean === false && <span className="review-marker">flagged</span>}{take.reused && <span className="hint">reused</span>}</>
                  : <span className="hint">{take?.state || '—'}</span>}</span>
              );
            })}
          </div>
        ))}
      </div>
      {plan.missing.length > 0 && <p className="hint">Missing performance types: {plan.missing.join(', ')}.</p>}
      <div className="studio-actions">{castable.map(c => (
        <span key={c.name} className="studio-cast-picks">
          <button type="button" className="btn btn-secondary" data-pick={c.name} data-scope="episode" onClick={() => pick(c.name, 'episode', false)}>Cast {c.name} for this episode</button>
          <button type="button" className="btn btn-ghost" data-pick={c.name} data-scope="series" onClick={() => pick(c.name, 'series', false)}>…for the series</button>
          <button type="button" className="btn btn-primary" data-pick={c.name} data-scope="episode" data-apply="1" onClick={() => pick(c.name, 'episode', true)}>Cast and re-render only {a.character}</button>
        </span>
      ))}</div>
      <p className="hint" role="status" data-note>{note}</p>
    </section>
  );
}

export function StudioCast({ ctrl }) {
  const job = ctrl.jobId();
  const { data: cast, error } = useQuery(castingQuery(ctrl.sid));
  const { data: voices } = useQuery(studioVoicesQuery);
  const [affected, setAffected] = useState(null);
  useEffect(() => stopTake, []);
  if (error) return <p>{error.message}</p>;
  if (!cast || !voices) return <Loading>Loading the cast…</Loading>;
  const auditions = ctrl.state.overview.auditions || [];

  async function whoFollows(character) {
    try {
      setAffected(await api(`studio/sessions/${ctrl.sid}/casting/affected?character=${encodeURIComponent(character)}`));
    } catch (err) { ctrl.status(err.message); }
  }

  return (
    <>
      <section className="panel studio-card" aria-labelledby="castNow">
        <h3 id="castNow">Who speaks with which voice</h3>
        {cast.speakers.length ? (
          <div className="table-wrap"><table className="table"><thead><tr><th>Speaker</th><th>Voice</th><th>Decided at</th><th /></tr></thead><tbody>
            {cast.speakers.map(r => (
              <tr key={r.speaker}>
                <td><strong>{r.speaker}</strong>{r.character !== r.speaker && <span className="hint"> ({r.character})</span>}</td>
                <td className="m">{r.voice || '—'}{r.engine && <span className="hint"> {r.engine}</span>}
                  {shapeLabel(r) && <span className="tag tag-neutral"> {shapeLabel(r)}</span>}</td>
                <td>{r.source === 'none' ? <span className="hint">not decided — the run&apos;s own cast applies</span>
                  : <span className={`tag ${r.source === 'episode' ? 'tag-accent' : 'tag-neutral'}`}>{r.source}</span>}
                  {r.overrides_series && <span className="review-marker">overrides the series choice</span>}</td>
                <td>{r.source === 'series' && <button type="button" className="btn btn-ghost" data-affected={r.character}
                  onClick={() => whoFollows(r.character)}>Who follows this</button>}</td>
              </tr>
            ))}
          </tbody></table></div>
        ) : <p className="hint">{job ? 'This run has no speakers yet.' : 'Render a draft to discover the speakers.'}</p>}
        {cast.line_exceptions.length > 0 && <p className="hint">Line exceptions: {cast.line_exceptions.map(l => `${l.cue_id} → ${l.voice}`).join(', ')}</p>}
        <div id="castAffected">{affected && (
          <div className="studio-import"><p>{affected.note}</p>
            <p>Would follow a new series choice: {affected.follow.map(f => shortRef(f.episode)).join(', ') || 'none'}</p>
            <p>Keep their own: {affected.keep.map(f => `${shortRef(f.episode)} (${f.voice || ''})`).join(', ') || 'none'}</p></div>
        )}</div>
      </section>

      <section className="panel studio-card" aria-labelledby="castAudition">
        <h3 id="castAudition">Audition a character</h3>
        {job ? <AuditionForm ctrl={ctrl} cast={cast} voices={voices} />
          : <p className="hint">Auditions use a finished run&apos;s lines. Render a draft first.</p>}
      </section>

      {auditions.map(a => <Audition key={a.id} ctrl={ctrl} id={a.id} character={a.character} />)}
    </>
  );
}
