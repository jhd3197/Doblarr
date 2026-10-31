import { useEffect, useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { libraryQuery, queryClient } from '../../lib/queries.js';
import { api } from '../../lib/api.js';

// Title knowledge: what an episode's lines establish (who is who, what
// happens, how characters speak), reviewed by a person before it is used.
// Extraction is started from an analysed episode's Analysis tab; here a
// person reviews proposals, activates them as one revision, and retires what
// no longer holds. Jobs already queued keep the revision they froze.

const KIND = { character: 'Character', alias: 'Alias', relationship: 'Relationship', event: 'Event',
  location: 'Location', scene_intent: 'Scene intent', addressee: 'Addressee', behavior: 'Behaviour',
  speech_mode: 'Speech mode', term: 'Term', summary: 'Summary' };
const DECISION = { accept: 'Accepted', edit: 'Corrected', reject: 'Rejected', defer: 'Deferred' };
const clock = ms => `${Math.floor(ms / 60000)}:${String(Math.floor(ms / 1000) % 60).padStart(2, '0')}`;
const order = o => `S${String(o[0]).padStart(2, '0')}E${String(o[1]).padStart(2, '0')}`;

export function seriesChoices(items) {
  const shows = (items || []).filter(i => i.media_type === 'show' && i.tvdb_id)
    .map(i => ({ id: `show:tvdb:${i.tvdb_id}`, label: i.title }));
  const movies = (items || []).filter(i => i.media_type === 'movie' && i.tmdb_id)
    .map(i => ({ id: `movie:tmdb:${i.tmdb_id}`, label: i.title }));
  return [...shows, ...movies].sort((a, b) => a.label.localeCompare(b.label));
}

const subjectOf = names => s => (s.startsWith('character:') ? (names[s.slice(10)] || s.slice(10)) : s.split(':').pop());

// `view` keeps the chosen series across tab switches (the page's state).
export function Narrative({ view, setView }) {
  const { data: library, isPending } = useQuery(libraryQuery);
  const choices = seriesChoices(library?.items);
  const [picked, setPicked] = useState(view.series || '');
  const [typed, setTyped] = useState(view.series || '');
  const open = series => { if (series) setView({ ...view, series }); };

  return (
    <div className="panel narrative-panel">
      <div className="narrative-pick">
        <label className="review-field">Show or film<select className="input" data-series value={picked}
          onChange={e => { setPicked(e.target.value); setTyped(e.target.value); open(e.target.value); }}>
          <option value="">{isPending ? 'Loading the library…' : 'Choose one'}</option>
          {choices.map(c => <option key={c.id} value={c.id}>{c.label}</option>)}
        </select></label>
        <label className="review-field">Or a series id<input className="input m" data-series-id value={typed}
          placeholder="show:tvdb:12345" onChange={e => setTyped(e.target.value)} /></label>
        <button type="button" className="btn btn-secondary" data-open onClick={() => open(typed.trim() || picked)}>Open</button>
      </div>
      <p className="hint">Proposals come from analysed episodes and are never used until you accept them and activate a revision.</p>
      <div data-narrative>{view.series && <Series key={view.series} series={view.series} />}</div>
    </div>
  );
}

function Series({ series }) {
  const key = ['narrative', series];
  const { data, error, isPending } = useQuery({ queryKey: key, staleTime: 0,
    queryFn: () => api(`narrative?series_id=${encodeURIComponent(series)}`) });
  const [draft, setDraft] = useState('');
  const [status, setStatus] = useState('');
  if (isPending) return <p className="hint">Reading title knowledge…</p>;
  if (error) return <p className="hint">{error.message}</p>;
  const subject = subjectOf(data.characters || {});
  const refresh = () => queryClient.invalidateQueries({ queryKey: key });

  async function activate(retire) {
    try {
      const result = await api('narrative/activate', { method: 'POST', json: {
        series_id: series, base_revision: data.revision, retire: retire ? [retire] : [] } });
      await refresh();
      setStatus(`Revision ${result.revision} is active with ${result.claims} claims. ${result.note}`);
    } catch (err) { setStatus(err.message); }
  }

  return (
    <>
      <h4>Episodes</h4>
      {data.coverage.length ? (
        <div className="table-wrap"><table className="table"><thead><tr><th>Episode</th><th>Draft</th><th>Proposals</th><th>To review</th><th>Conflicts</th><th>Active</th><th></th></tr></thead><tbody>
          {data.coverage.map(r => (
            <tr key={r.draft_id}><td className="m">{r.order ? order(r.order) : r.media_id}</td>
              <td>{r.state}<span className="hint"> · {r.model}</span></td><td className="m">{r.candidates}</td>
              <td className="m">{r.needs_review}{r.stale ? <> <span className="hint">({r.stale} changed)</span></> : ''}</td>
              <td className="m">{r.conflicts}</td><td className="m">{r.active_claims}</td>
              <td><button type="button" className="btn btn-ghost" data-draft={r.draft_id} onClick={() => setDraft(r.draft_id)}>Review</button></td></tr>
          ))}</tbody></table></div>
      ) : <p className="hint">No episode of this series has proposals yet. Analyse an episode, then use Extract knowledge on its Analysis tab.</p>}
      <div data-draft-box>{draft && <Draft key={draft} draftId={draft} subject={subject} onChange={refresh} />}</div>
      <h4>Active knowledge <span className="hint">revision {data.revision}</span></h4>
      <div className="studio-actions"><button type="button" className="btn btn-primary" data-activate onClick={() => activate(null)}>Activate reviewed knowledge</button>
        <span className="hint">Creates revision {data.revision + 1}. New dubs use it; queued and finished dubs keep theirs.</span></div>
      <p className="hint" role="status" data-status>{status}</p>
      {data.claims.length ? (
        <div className="table-wrap"><table className="table"><thead><tr><th>Kind</th><th>Statement</th><th>About</th><th>From</th><th></th></tr></thead><tbody>
          {data.claims.map(c => (
            <tr key={c.id}><td>{KIND[c.kind] || c.kind}{c.conflicted && <> <span className="tag tag-outline" title="Another accepted claim contradicts this one; neither is used until you retire one">conflict</span></>}</td>
              <td>{c.statement}{c.corrected && <> <span className="hint">(your correction)</span></>}</td>
              <td>{(c.subjects || []).map(subject).join(', ') || '—'}</td>
              <td className="m">{c.available_from ? `S${c.available_from[0]}E${c.available_from[1]}` : 'this film'}{c.revealed_from ? ` · known from S${c.revealed_from[0]}E${c.revealed_from[1]}` : ''}</td>
              <td><button type="button" className="btn btn-ghost" data-retire={c.id} onClick={() => activate(c.id)}>Retire</button></td></tr>
          ))}</tbody></table></div>
      ) : <p className="hint">Nothing is active yet.</p>}
      {data.external.length > 0 && <><h4>From library metadata and title research</h4>
        {data.external.map((e, n) => (
          <div key={n} className="hint narrative-external">
            <p>{String(e.source || '').startsWith('research:') && <span className="tag tag-outline">title research</span>}{' '}
              {e.statement} · {e.source} · {e.fetched_at} (not evidence from the episode)</p>
            {e.sources?.length > 0 && <p>Cited: {e.sources.map((u, i) => <span key={u}>{i ? ', ' : ''}<a href={u} target="_blank" rel="noreferrer">{(() => { try { return new URL(u).hostname; } catch { return u; } })()}</a></span>)}</p>}
          </div>
        ))}</>}
    </>
  );
}

function Draft({ draftId, subject, onChange }) {
  const key = ['narrative-draft', draftId];
  const { data, error, isPending } = useQuery({ queryKey: key, staleTime: 0,
    queryFn: () => api(`narrative/draft/${encodeURIComponent(draftId)}`) });
  const [status, setStatus] = useState('');
  if (isPending) return <p className="hint">Reading the proposals…</p>;
  if (error) return <p className="hint">{error.message}</p>;
  const refresh = () => Promise.all([queryClient.invalidateQueries({ queryKey: key }), onChange()]);
  return (
    <div className="panel narrative-draft">
      <h4>Proposals <span className="hint">{data.candidates.length} · extracted by {data.model} · {data.state}</span></h4>
      <p className="hint">Each proposal cites the lines it rests on. A correction replaces the wording and survives later extractions.</p>
      {data.candidates.map(row => <ClaimRow key={row.candidate_id} row={row} draftId={draftId} subject={subject} onDone={refresh} say={setStatus} />)}
      <p className="hint" role="status" data-draft-status>{status}</p>
    </div>
  );
}

function ClaimRow({ row, draftId, subject, onDone, say }) {
  const p = row.proposal;
  const [editing, setEditing] = useState(false);
  const [correction, setCorrection] = useState(row.review?.correction || p.statement);
  const done = row.review && !row.stale ? DECISION[row.review.decision] : '';
  const field = useRef(null);
  useEffect(() => { if (editing) field.current?.focus(); }, [editing]);
  async function decide(decision) {
    if (decision === 'edit' && !editing) { setEditing(true); return; }
    try {
      await api('narrative/review', { method: 'POST', json: {
        draft_id: draftId, candidate_id: row.candidate_id, decision,
        correction: decision === 'edit' ? correction.trim() : null,
        expected_revision: Number(row.review_revision) } });
      await onDone();
    } catch (error) { say(error.message); }
  }
  return (
    <div className="narrative-claim" data-candidate={row.candidate_id} data-revision={row.review_revision}>
      <p><strong>{KIND[p.kind] || p.kind}</strong> {p.statement}
        {p.conflict_group && <> <span className="tag tag-outline">alternatives exist</span></>}
        {done ? <> <span className="tag tag-neutral">{done}</span></> : row.stale && <> <span className="tag tag-outline">changed since your review</span></>}</p>
      <p className="hint">{(p.subjects || []).map(subject).join(', ') || 'no named subject'}{p.confidence != null ? ` · model score ${p.confidence} (uncalibrated)` : ''} · {(p.uncertainties || []).join(' ')}</p>
      <details><summary className="hint">{row.evidence.length} supporting line{row.evidence.length === 1 ? '' : 's'}</summary>
        {row.evidence.map((e, n) => <p key={n} className="hint"><span className="m">{clock(e.start_ms)}</span> {e.text}</p>)}</details>
      <div className="studio-actions">
        <button type="button" className="btn btn-ghost" data-decide="accept" onClick={() => decide('accept')}>Accept</button>
        <button type="button" className="btn btn-ghost" data-decide="edit" onClick={() => decide('edit')}>Correct</button>
        <button type="button" className="btn btn-ghost" data-decide="reject" onClick={() => decide('reject')}>Reject</button>
        <button type="button" className="btn btn-ghost" data-decide="defer" onClick={() => decide('defer')}>Later</button>
        <input className="input narrative-correction" maxLength={400} placeholder="Corrected statement" hidden={!editing}
          value={correction} onChange={e => setCorrection(e.target.value)} ref={field} />
      </div>
    </div>
  );
}
