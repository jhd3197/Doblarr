import { useEffect, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { ensure, queryClient } from '../../lib/queries.js';
import { api } from '../../lib/api.js';
import { baseLanguage as baseOf, languageName } from '../../lib/languages.js';
import { entriesQuery, pageCount } from '../../lib/knowledge.js';
import { KnowledgeCorrection, useVoiceSample } from '../../components/KnowledgeCorrection.jsx';
import { PageHeader } from '../../components/PageHeader.jsx';
import { catalogQuery, coverageQuery } from './queries.js';
import { Narrative } from './Narrative.jsx';
import { Templates } from './Templates.jsx';

const STATUS_LABEL = { proposed: 'Unreviewed', reviewed: 'Reviewed', 'needs-retest': 'Needs retest', retired: 'Retired' };
const TABS = [
  ['pronunciation', 'Pronunciations'],
  ['term', 'Terminology & phrases'],
  ['memory', 'Translation memory'],
  ['packs', 'Installed packs'],
  ['narrative', 'Title knowledge'],
  ['templates', 'Audio templates'],
];
const ENTRY_TABS = new Set(['pronunciation', 'term']);

// The page's filters live for the session, as they did in the vanilla page.
const session = { view: { tab: 'pronunciation', locale: '', scope: '', status: '', q: '', page: 1, pageSize: 25 } };

const listQuery = view => ({ queryKey: ['knowledge', 'entries', view.tab, view.locale, view.scope, view.status, view.q, view.page],
  queryFn: () => api(entriesQuery({ ...view, kind: view.tab })), staleTime: 0 });

export async function knowledgeLoader() {
  await Promise.all([ensure(coverageQuery),
    ENTRY_TABS.has(session.view.tab) ? ensure(listQuery(session.view)).catch(() => null) : null]);
  return null;
}

function StatusTag({ status }) {
  return status === 'proposed' ? <span className="tag tag-accent">Unreviewed</span>
    : <span className="tag tag-neutral">{STATUS_LABEL[status] || status}</span>;
}

function Memory({ view, setView }) {
  const key = ['knowledge', 'memory', view.page];
  const { data, error, isPending } = useQuery({ queryKey: key, queryFn: () => api(`memory?page=${view.page}`), staleTime: 0 });
  const [failed, setFailed] = useState({});
  if (isPending) return null;
  if (error) return <>{error.message}</>;
  async function retire(id) {
    try { await api(`memory/${encodeURIComponent(id)}/retire`, { method: 'POST' }); await queryClient.invalidateQueries({ queryKey: key }); }
    catch (e) { setFailed({ ...failed, [id]: e.message }); }
  }
  return (
    <div className="panel knowledge-panel">
      <p className="hint">Private complete lines saved from dialogue review. Enable reuse in Translation settings. Unknown context and unreviewed lines cannot bypass translation.</p>
      {data.entries.length ? data.entries.map(e => (
        <p key={e.id}><strong>{e.source_text}</strong> → {e.target_text} <span className="hint">{e.source_lang} → {e.target_locale} · {e.status} · revision {e.revision}</span>
          {' '}<button className="btn btn-ghost memory-retire" disabled={e.status === 'retired'} onClick={() => retire(e.id)}>{failed[e.id] || 'Retire'}</button></p>
      )) : <p>No translations saved yet.</p>}
      <button className="btn btn-ghost memory-prev" disabled={view.page <= 1} onClick={() => setView({ ...view, page: view.page - 1 })}>Previous</button>
      <span>{view.page} / {pageCount(data.total, 25)}</span>
      <button className="btn btn-ghost memory-next" disabled={view.page * 25 >= data.total} onClick={() => setView({ ...view, page: view.page + 1 })}>Next</button>
    </div>
  );
}

function Packs() {
  const { data, error, isPending } = useQuery({ queryKey: ['knowledge', 'packs'], queryFn: () => api('packs'), staleTime: 0 });
  const [path, setPath] = useState('');
  const [packId, setPackId] = useState('');
  const [status, setStatus] = useState('');
  const [busy, setBusy] = useState('');
  if (isPending) return null;
  if (error) return <>{error.message}</>;
  async function perform(which, url, payload) {
    setBusy(which);
    try { await api(url, { method: 'POST', json: payload }); await queryClient.invalidateQueries({ queryKey: ['knowledge'] }); setBusy(''); }
    catch (e) { setStatus(e.message); setBusy(''); }
  }
  return (
    <div className="panel knowledge-panel">
      <p className="hint">Pack updates affect new jobs. Existing jobs retain their pinned revisions. Proposed content stays inactive.</p>
      <label className="review-field">Local pack JSON path<input className="input pack-path" value={path} onChange={e => setPath(e.target.value)} /></label>
      <button className="btn btn-secondary pack-install" disabled={busy === 'install'} onClick={() => perform('install', 'packs/install', { path })}>Install file</button>
      <label className="review-field">Pack ID from configured distribution<input className="input pack-id" value={packId} onChange={e => setPackId(e.target.value)} /></label>
      <button className="btn btn-secondary pack-download" disabled={busy === 'download'} onClick={() => perform('download', 'packs/download', { pack_id: packId })}>Download and install</button>
      <p className="pack-status hint" role="status">{status}</p>
      {data.packs.length ? data.packs.map(p => (
        <div key={p.pack_id}>
          <h4>{p.name} · {p.third_party ? 'Third-party' : 'Official'}</h4>
          {p.releases.map(r => <p key={r.release} className="hint">{r.release} · {r.active ? 'Active' : 'Retained'} · {JSON.stringify(r.coverage)}</p>)}
          <button className="btn btn-ghost pack-rollback" disabled={p.releases.length < 2 || busy === p.pack_id}
            onClick={() => perform(p.pack_id, `packs/${encodeURIComponent(p.pack_id)}/rollback`, {})}>Roll back</button>
        </div>
      )) : <p>No packs installed.</p>}
    </div>
  );
}

function EntryDetail({ id }) {
  const { data, error, isPending } = useQuery({ queryKey: ['knowledge', 'entry', id], queryFn: () => api(`knowledge/entries/${id}`), staleTime: 0 });
  const { data: voices = [] } = useQuery(catalogQuery);
  const sample = useVoiceSample();
  const [sentence, setSentence] = useState(null);
  const [voice, setVoice] = useState('');
  const [status, setStatus] = useState('');
  if (isPending) return <p className="hint">Loading entry…</p>;
  if (error) return <p className="hint">{error.message}</p>;
  const e = data.entry;
  const choices = voices.filter(v => !e.locale || baseOf(e.locale) === String(v.language || '').toLowerCase());
  const chosen = voice || choices[0]?.key || '';
  async function retire() {
    try {
      await api(`knowledge/entries/${id}/retire`, { method: 'POST' });
      setStatus('Retired. Future dubs will not use it; frozen jobs are unchanged.');
      queryClient.invalidateQueries({ queryKey: ['knowledge', 'entries'] });
    } catch (err) { setStatus(err.message); }
  }
  return (
    <div className="panel knowledge-panel knowledge-entry">
      <div className="knowledge-entry-head">
        <h3>{e.phrase || '(suppression)'}</h3>
        <span className="m hint">{e.kind} · {languageName(e.locale)} · {e.scope}{e.scope_ref ? ` · ${e.scope_ref}` : ''}{e.register ? ` · ${e.register}` : ''} · revision {e.revision} · {STATUS_LABEL[e.status] || e.status}{e.origin === 'installed' ? ' · installed pack' : ''}</span>
        <span className="episode-actions knowledge-entry-actions">
          <button className="btn btn-ghost knowledge-retire" disabled={e.status === 'retired'} onClick={retire}>Retire</button>
        </span></div>
      {e.pronunciation && <p className="knowledge-sound">Intended sound: {e.pronunciation}{e.ipa && <> <span className="m">/{e.ipa}/</span></>}</p>}
      {e.usage && <p className="hint knowledge-line">Usage: {e.usage}</p>}
      {e.examples.length > 0 && <p className="hint knowledge-line">Examples: {e.examples.join(' · ')}</p>}
      {e.suppresses && <p className="hint knowledge-line">Suppresses rule {e.suppresses}</p>}
      {data.suppressed_by.length > 0 && <p className="hint knowledge-line">Suppressed by: {data.suppressed_by.map(s => s.phrase || s.id).join(', ')}</p>}
      <h4 className="knowledge-subhead">Engine realizations ({data.realizations.length})</h4>
      {data.realizations.length ? (
        <div className="table-wrap"><table className="table"><thead><tr><th>Replacement</th><th>Engine</th><th>Model</th><th>Voice</th><th>Status</th></tr></thead>
          <tbody>{data.realizations.map((r, n) => (
            <tr key={n}><td className="knowledge-strong">{r.replacement}</td>
              <td className="m">{r.engine}</td><td className="m">{r.model ?? 'any (broad)'}</td>
              <td className="m">{r.voice ?? 'any'}</td><td><StatusTag status={r.status} /></td></tr>
          ))}</tbody></table></div>
      ) : <p className="hint">No tested realization yet — this rule stays silent for every engine until one is added.</p>}
      <div className="knowledge-listen-row">
        <label className="review-field">Audition sentence<input className="input knowledge-sample" value={sentence ?? e.phrase}
          onChange={ev => setSentence(ev.target.value)} /></label>
        <label className="review-field">Voice<select className="input knowledge-voice" value={chosen} onChange={ev => setVoice(ev.target.value)}>
          {choices.map(v => <option key={v.key} value={v.key}>{v.name} ({v.engine})</option>)}</select></label>
        <button className="btn btn-secondary knowledge-listen" disabled={!choices.length || sample.busy}
          onClick={() => sample.listen({ key: chosen, text: sentence ?? e.phrase, language: baseOf(e.locale) || 'en' })}>Listen</button>
      </div>
      <audio controls className="knowledge-audio" hidden={!sample.src} src={sample.src || undefined} />
      <p className="knowledge-status hint" role="status">{status || sample.status}</p>
      <details className="knowledge-history"><summary>Review history ({e.review_history.length})</summary>
        <pre>{e.review_history.length ? JSON.stringify(e.review_history, null, 2) : 'No recorded review yet.'}</pre></details>
    </div>
  );
}

function Entries({ view, setView, locales }) {
  const [draft, setDraft] = useState({ q: view.q, locale: view.locale, scope: view.scope, status: view.status });
  const [selected, setSelected] = useState('');
  const { data, error, isPending } = useQuery(listQuery(view));
  const set = key => e => setDraft({ ...draft, [key]: e.target.value });
  const options = locales.some(c => c.locale === draft.locale) || !draft.locale ? locales : [...locales, { locale: draft.locale, name: draft.locale }];
  const pages = data ? pageCount(data.total, view.pageSize) : 1;
  return (
    <>
      <div className="panel knowledge-filters"><div className="knowledge-filter-row">
        <label className="review-field">Search<input className="input knowledge-q" type="search" value={draft.q} onChange={set('q')}
          placeholder="Phrase, source wording, usage" /></label>
        <label className="review-field">Locale<select className="input knowledge-locale" value={draft.locale} onChange={set('locale')}>
          <option value="">All locales</option>
          {options.map(c => <option key={c.locale} value={c.locale}>{c.name}</option>)}</select></label>
        <label className="review-field">Scope<select className="input knowledge-scope" value={draft.scope} onChange={set('scope')}>
          {['', 'line', 'episode', 'movie', 'show', 'personal', 'pack'].map(s => <option key={s} value={s}>{s || 'All scopes'}</option>)}</select></label>
        <label className="review-field">Status<select className="input knowledge-status" value={draft.status} onChange={set('status')}>
          {['', 'proposed', 'reviewed', 'needs-retest', 'retired'].map(s => <option key={s} value={s}>{s ? STATUS_LABEL[s] : 'All statuses'}</option>)}</select></label>
        <button className="btn btn-secondary knowledge-search" onClick={() => setView({ ...view, ...draft, q: draft.q.trim(), page: 1 })}>Search</button>
      </div></div>
      <div className="knowledge-list">
        {isPending ? <p className="hint">Searching…</p> : error ? <p className="hint">{error.message}</p> : (
          <>
            <div className="panel activity-panel">
              <div className="table-wrap"><table className="table"><thead><tr><th>Phrase</th><th className="knowledge-col-locale">Locale</th>
                <th className="knowledge-col-scope">Scope</th><th className="knowledge-col-status">Status</th><th className="knowledge-col-rules">Rules</th></tr></thead>
                <tbody>{data.entries.length ? data.entries.map(e => (
                  <tr key={e.id} className="knowledge-row" data-id={e.id} onClick={() => setSelected(e.id)}>
                    <td className="knowledge-strong">{e.phrase || '(suppression)'}{e.sense && <> <span className="hint">· {e.sense}</span></>}{e.register && <> <span className="hint">· {e.register}</span></>}</td>
                    <td className="m knowledge-cell">{languageName(e.locale)}</td>
                    <td className="m knowledge-cell">{e.scope}</td>
                    <td><StatusTag status={e.status} /></td>
                    <td className="m knowledge-cell">v{e.revision}</td></tr>
                )) : <tr><td colSpan="5" className="hint">No entries match. Add a correction above.</td></tr>}</tbody></table></div></div>
            <div className="episode-actions knowledge-pager">
              <button className="btn btn-ghost knowledge-prev" disabled={view.page <= 1} onClick={() => setView({ ...view, page: view.page - 1 })}>← Newer</button>
              <span className="hint">Page {view.page} of {pages} · {data.total} entries</span>
              <button className="btn btn-ghost knowledge-next" disabled={view.page >= pages} onClick={() => setView({ ...view, page: view.page + 1 })}>Older →</button>
            </div>
          </>
        )}
      </div>
      <div className="knowledge-detail">{selected && <EntryDetail key={selected} id={selected} />}</div>
    </>
  );
}

export function Knowledge() {
  const [view, setView] = useState(session.view);
  useEffect(() => { session.view = view; }, [view]);
  const { data: coverage = { locales: [] } } = useQuery(coverageQuery);
  const [correcting, setCorrecting] = useState('');
  const openTab = tab => setView({ ...view, tab, page: 1 });
  const onSaved = () => queryClient.invalidateQueries({ queryKey: ['knowledge'] });

  return (
    <div className="page" id="knowledgeRoot">
      <PageHeader title="Language knowledge" hint="Corrections you save apply to new dubs; existing jobs keep their frozen rules." />
      <div className="knowledge-cards">
        {coverage.locales.map(c => (
          <div key={c.locale} className="panel knowledge-card">
            <p className="knowledge-card-name">{c.name}</p>
            <p className="m knowledge-card-locale">{c.locale}</p>
            <p className="hint knowledge-card-counts">{c.reviewed} reviewed · {c.proposed} proposed{c['needs-retest'] ? ` · ${c['needs-retest']} needs retest` : ''}</p>
            <div className="episode-actions knowledge-card-actions">
              <button className="btn btn-secondary knowledge-add" data-locale={c.locale} onClick={() => setCorrecting(c.locale)}>Add a correction</button>
            </div>
          </div>
        ))}
      </div>
      <div className="knowledge-tabs">
        {TABS.map(([k, t]) => (
          <button key={k} type="button" className="tab knowledge-tab" data-tab={k} aria-current={view.tab === k ? 'page' : 'false'}
            onClick={() => openTab(k)}>{t}</button>
        ))}
      </div>
      <div className="knowledge-body">
        {view.tab === 'memory' && <Memory view={view} setView={setView} />}
        {view.tab === 'packs' && <Packs />}
        {view.tab === 'narrative' && <Narrative view={view} setView={setView} />}
        {view.tab === 'templates' && <Templates view={view} setView={setView} />}
        {ENTRY_TABS.has(view.tab) && <Entries key={view.tab} view={view} setView={setView} locales={coverage.locales} />}
      </div>
      {correcting && <KnowledgeCorrection kind="pronunciation" locale={correcting} onSaved={onSaved} onClose={() => setCorrecting('')} />}
    </div>
  );
}
