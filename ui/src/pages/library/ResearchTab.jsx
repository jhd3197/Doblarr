import { useEffect, useEffectEvent, useState } from 'react';
import { Link } from 'react-router';
import { useQuery } from '@tanstack/react-query';
import { api } from '../../lib/api.js';
import { useJobs } from '../../lib/jobs.js';
import { queryClient } from '../../lib/queries.js';
import { isShow } from '../../lib/library.js';
import { citedParts, languageColumns, seriesIdOf, voicesIn } from '../../lib/research.js';
import { Loading, EmptyState } from '../../components/States.jsx';
import './research.css';

const SCRIPT_SOURCES = [
  ['fandom', 'Fandom transcripts'], ['screenplays', 'Screenplay archives'],
  ['kitsunekko', 'Japanese subtitle mirror'], ['opensubtitles', 'OpenSubtitles'],
  ['dubbing', 'Dubbing Database'],
];

const castKey = series => ['published-cast', series];
const runsKey = series => ['research-runs', series];
const scriptsKey = series => ['research-scripts', series];
const titlesKey = series => ['research-titles', series];

// A queued research job refreshes what it writes when it finishes.
function useWhenDone(jobId, keys) {
  const { data } = useJobs();
  const job = (data?.jobs || []).find(j => j.id === jobId);
  const status = job?.status;
  // Reads the latest keys when the job ends, without re-running on every new array.
  const refresh = useEffectEvent(() => keys.forEach(key => queryClient.invalidateQueries({ queryKey: key })));
  useEffect(() => {
    if (status === 'done' || status === 'failed') refresh();
  }, [status]);
  return job;
}

function Status({ children, id }) {
  return <span className="hint research-status" role="status" id={id}>{children}</span>;
}

function SourceBadge({ name }) {
  return <span className={`research-badge research-badge-${name}`} title={`listed by ${name}`}>{name}</span>;
}

function CastLinks({ series, cast, enabled }) {
  const [status, setStatus] = useState('');
  async function refresh(link) {
    setStatus(`Fetching ${link.source} again…`);
    try {
      const saved = await api('published-cast/refresh', { method: 'POST', json: { series_id: series, season: link.season, source: link.source } });
      setStatus(`${link.source}: ${saved.characters} characters.`);
      queryClient.invalidateQueries({ queryKey: castKey(series) });
    } catch (err) { setStatus(err.message); }
  }
  async function importCast(roles) {
    setStatus('Importing…');
    try {
      const done = await api('published-cast/import', { method: 'POST', json: { series_id: series, roles } });
      const ambiguous = done.ambiguous.length ? ` · ${done.ambiguous.length} left for you (the name fits several)` : '';
      setStatus(`Created ${done.created.length}, matched ${done.matched.length}${ambiguous}.`);
      queryClient.invalidateQueries({ queryKey: ['characters'] });
    } catch (err) { setStatus(err.message); }
  }
  const links = cast?.links || [];
  return (
    <div className="research-block">
      {links.length ? (
        <ul className="research-links" id="researchLinks">
          {links.map(link => (
            <li key={link.id}>
              <SourceBadge name={link.source} />
              {link.url ? <a href={link.url} target="_blank" rel="noreferrer">{link.title || link.url}</a> : <span>{link.title}</span>}
              <span className="note">{link.characters} characters{link.complete ? '' : ' (more exist)'}
                {link.season != null ? ` · season ${link.season}` : ''}</span>
              {link.why && <span className="note">· {link.why}</span>}
              {enabled && link.source !== 'research' &&
                <button type="button" className="btn btn-ghost research-mini" onClick={() => refresh(link)}>Refresh</button>}
            </li>
          ))}
        </ul>
      ) : <EmptyState>No published cast linked yet. Search a catalogue below and link the entry that is this title.</EmptyState>}
      {links.length > 0 && (
        <div className="research-row">
          <button type="button" className="btn btn-secondary" id="researchImportMain" onClick={() => importCast(['MAIN'])}>Import main cast</button>
          <button type="button" className="btn btn-ghost" onClick={() => importCast(['MAIN', 'SUPPORTING'])}>Main and supporting</button>
          <span className="note">Creates series characters or adds the published facts to ones you already have. Never renames.</span>
        </div>
      )}
      <Status>{status}</Status>
    </div>
  );
}

function CastSearch({ series, title, catalogues, linked }) {
  const [query, setQuery] = useState(title || '');
  const [source, setSource] = useState('anilist');
  const [found, setFound] = useState(null);
  const [status, setStatus] = useState('');
  async function search(e) {
    e.preventDefault();
    setStatus('Searching…');
    try {
      const data = await api(`published-cast/search?series_id=${encodeURIComponent(series)}&query=${encodeURIComponent(query)}&source=${source}`);
      setFound({ hits: data.hits, skipped: data.skipped });
      setStatus(`Sent "${data.query}" to ${data.sent_to.join(', ')}.`);
    } catch (err) { setStatus(err.message); }
  }
  async function suggest(name) {
    setStatus(`Looking for this title on ${name}…`);
    try {
      const data = await api(`published-cast/suggest?series_id=${encodeURIComponent(series)}&source=${name}`);
      setFound({ hits: { [name]: data.hits }, skipped: {} });
      setStatus('Ranked by matching title, year and episode count. Link the one that is this title.');
    } catch (err) { setStatus(err.message); }
  }
  async function link(hit) {
    setStatus(`Linking ${hit.title}…`);
    try {
      const saved = await api('published-cast/link', { method: 'POST', json: { series_id: series, url: hit.url, why: hit.why || '' } });
      setStatus(`Linked ${saved.title}: ${saved.characters} characters${saved.complete ? '' : ' (more exist)'}.`);
      queryClient.invalidateQueries({ queryKey: castKey(series) });
    } catch (err) { setStatus(err.message); }
  }
  const others = catalogues.filter(c => !linked.includes(c.name));
  return (
    <div className="research-block">
      <form className="research-row" onSubmit={search} id="researchCastSearch">
        <input className="input research-grow" value={query} onChange={e => setQuery(e.target.value)} placeholder="Title to search" aria-label="Title to search" />
        <select className="input" value={source} onChange={e => setSource(e.target.value)} aria-label="Catalogue">
          {catalogues.map(c => <option key={c.name} value={c.name}>{c.label}</option>)}
          <option value="all">Every catalogue</option>
        </select>
        <button type="submit" className="btn btn-secondary">Search</button>
      </form>
      {linked.length > 0 && others.length > 0 && (
        <div className="research-row">
          <span className="note">Find the linked title in</span>
          {others.map(c => <button key={c.name} type="button" className="btn btn-ghost research-mini" onClick={() => suggest(c.name)}>{c.label}</button>)}
        </div>
      )}
      <Status>{status}</Status>
      {found && Object.entries(found.hits).map(([name, hits]) => (
        <div key={name} className="research-hits">
          <p className="research-hits-head">{catalogues.find(c => c.name === name)?.label || name}</p>
          {hits.length ? hits.map(hit => (
            <div key={hit.url} className="research-hit">
              <a href={hit.url} target="_blank" rel="noreferrer">{hit.title}</a>
              <span className="note">{[hit.format, hit.year, hit.episodes && `${hit.episodes} eps`].filter(Boolean).join(' · ')}</span>
              {hit.why && <span className="research-why">{hit.why}</span>}
              <button type="button" className="btn btn-ghost research-mini" onClick={() => link(hit)}>Link</button>
            </div>
          )) : <EmptyState>Nothing found.</EmptyState>}
        </div>
      ))}
      {found && Object.entries(found.skipped).map(([name, why]) => (
        <p key={name} className="note">{name}: skipped ({why})</p>
      ))}
    </div>
  );
}

function MergedCast({ cast, target }) {
  const rows = cast?.characters || [];
  const languages = languageColumns(rows, target);
  if (!rows.length) return null;
  return (
    <div className="research-table-wrap">
      <table className="table research-table" id="researchMergedCast">
        <thead>
          <tr><th>Character</th><th>Role</th><th>Listed by</th>{languages.map(l => <th key={l}>{l || 'Language not stated'}</th>)}</tr>
        </thead>
        <tbody>
          {rows.map(c => (
            <tr key={c.name + c.sources.join()}>
              <td>
                <span className="research-name">{c.name}</span>
                {c.conflicts.length > 0 && <span className="research-conflict" title="the catalogues disagree; nothing was chosen"> sources disagree on {c.conflicts.join(', ')}</span>}
              </td>
              <td className="note">{(c.role || '').toLowerCase() || '—'}</td>
              <td>{c.sources.map(s => <SourceBadge key={s} name={s} />)}</td>
              {languages.map(l => (
                <td key={l}>{voicesIn(c, l).map(v => (
                  <span key={v.name} className="research-voice" title={`per ${v.sources.join(', ')}`}>{v.name}</span>
                )) }</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Answer({ text, sources }) {
  return (
    <div className="research-answer">
      {citedParts(text, sources).map((block, i) => {
        const inline = block.parts.map((p, j) => (p.url
          ? <a key={j} href={p.url} target="_blank" rel="noreferrer" className="research-cite">[{p.n}]</a>
          : <span key={j}>{p.text}</span>));
        if (block.kind === 'heading') return <h4 key={i}>{inline}</h4>;
        if (block.kind === 'item') return <p key={i} className="research-item">• {inline}</p>;
        return <p key={i}>{inline}</p>;
      })}
    </div>
  );
}

function RunDetail({ runId }) {
  const { data: run, error } = useQuery({ queryKey: ['research-run', runId], queryFn: () => api(`research/runs/${runId}`) });
  const [status, setStatus] = useState('');
  if (error) return <p className="note">{error.message}</p>;
  if (!run) return <Loading />;
  async function decide(index, decision) {
    try {
      await api('research/leads', { method: 'POST', json: { run_id: runId, index, decision } });
      queryClient.invalidateQueries({ queryKey: ['research-run', runId] });
      queryClient.invalidateQueries({ queryKey: castKey(run.series_id) });
      setStatus(decision === 'accept' ? 'Added to the cast as a research lead.' : 'Dismissed.');
    } catch (err) { setStatus(err.message); }
  }
  const cited = run.sources.filter(s => s.n != null);
  return (
    <div className="research-run-detail">
      <p className="note">Sent: “{run.sent}” · {run.model} · {run.depth} · ${Number(run.cost || 0).toFixed(4)}</p>
      <Answer text={run.answer || '(no answer)'} sources={run.sources} />
      {cited.length > 0 && (
        <ol className="research-sources">
          {cited.map(s => <li key={s.url} value={s.n}><a href={s.url} target="_blank" rel="noreferrer">{s.title || s.url}</a>{s.cited ? '' : ' (read, not cited)'}</li>)}
        </ol>
      )}
      {run.gaps?.length > 0 && <p className="note">Not answered by what was read: {run.gaps.join('; ')}</p>}
      {run.claim_id && <p className="note">The answer waits in <Link to="/knowledge">Knowledge → Narrative</Link> as an external note, apart from what episodes taught.</p>}
      {run.terms?.length > 0 && (
        <div className="research-terms">
          <p className="research-hits-head">Terms for review</p>
          {run.terms.map(t => <p key={t.source_form + t.phrase}>{t.source_form} → <b>{t.phrase}</b> <span className="note">{t.usage}</span></p>)}
          <p className="note">{run.terms.some(t => t.entry_id) ? <>Proposed in <Link to="/knowledge">Knowledge → Terms</Link>; inactive until you review them.</> : 'Kept with the run only: this title has no show or film file to scope them to.'}</p>
        </div>
      )}
      {run.leads?.length > 0 && (
        <div className="research-leads">
          <p className="research-hits-head">Cast leads</p>
          {run.leads.map((lead, i) => (
            <div key={i} className="research-hit">
              <span>{lead.matches || lead.character} · {lead.language}: <b>{lead.voice_actor}</b></span>
              {lead.urls.map(u => <a key={u} href={u} target="_blank" rel="noreferrer" className="research-cite">source</a>)}
              {lead.state === 'open' ? (
                <>
                  <button type="button" className="btn btn-ghost research-mini" onClick={() => decide(i, 'accept')}>Accept</button>
                  <button type="button" className="btn btn-ghost research-mini" onClick={() => decide(i, 'dismiss')}>Dismiss</button>
                </>
              ) : <span className="note">{lead.state}</span>}
            </div>
          ))}
        </div>
      )}
      <Status>{status}</Status>
    </div>
  );
}

function Questions({ series, title, enabled }) {
  const { data } = useQuery({ queryKey: runsKey(series), queryFn: () => api(`research/runs?series_id=${encodeURIComponent(series)}`) });
  const [question, setQuestion] = useState('');
  const [depth, setDepth] = useState('');
  const [jobId, setJobId] = useState(null);
  const [open, setOpen] = useState(null);
  const [status, setStatus] = useState('');
  const job = useWhenDone(jobId, [runsKey(series)]);
  async function ask(e) {
    e.preventDefault();
    try {
      const queued = await api('research', { method: 'POST', json: { series_id: series, question, depth } });
      setJobId(queued.job_id);
      setStatus(`Queued with ${queued.model}. Sends: “${queued.sent}”.`);
    } catch (err) { setStatus(err.message); }
  }
  const runs = data?.runs || [];
  return (
    <div className="research-block">
      {enabled && (
        <form className="research-ask" onSubmit={ask} id="researchAsk">
          <textarea className="input" rows={2} value={question} maxLength={500} onChange={e => setQuestion(e.target.value)}
            placeholder={`For example: how does the Spanish dub of ${title || 'this title'} say the main characters' names?`} aria-label="Question" />
          <div className="research-row">
            <select className="input" value={depth} onChange={e => setDepth(e.target.value)} aria-label="Depth">
              <option value="">Depth from settings</option><option value="quick">Quick</option>
              <option value="standard">Standard</option><option value="deep">Deep</option>
            </select>
            <button type="submit" className="btn btn-primary research-ask-btn" disabled={question.trim().length < 3}>Ask</button>
            <span className="note">Only your question and the title leave this machine. What comes back waits for your review.</span>
          </div>
        </form>
      )}
      <Status>{status}{job && job.status !== 'queued' ? ` ${job.status}: ${job.message || ''}` : ''}</Status>
      {runs.length ? (
        <ul className="research-runs" id="researchRuns">
          {runs.map(run => (
            <li key={run.id}>
              <button type="button" className="research-run-head" aria-expanded={open === run.id}
                onClick={() => setOpen(open === run.id ? null : run.id)}>
                <span className="research-run-q">{run.question}</span>
                <span className="note">{new Date(run.created_at).toLocaleString()} · {run.sources} sources
                  {run.terms ? ` · ${run.terms} terms` : ''}{run.open_leads ? ` · ${run.open_leads} open leads` : ''}</span>
              </button>
              {open === run.id && <RunDetail runId={run.id} />}
            </li>
          ))}
        </ul>
      ) : <EmptyState>No questions asked about this title yet.</EmptyState>}
    </div>
  );
}

function Scripts({ series, enabled, show }) {
  const { data } = useQuery({ queryKey: scriptsKey(series), queryFn: () => api(`research/scripts?series_id=${encodeURIComponent(series)}`) });
  const [chosen, setChosen] = useState(['fandom', 'screenplays']);
  const [wiki, setWiki] = useState('');
  const [jobId, setJobId] = useState(null);
  const [status, setStatus] = useState('');
  const job = useWhenDone(jobId, [scriptsKey(series), runsKey(series)]);
  async function find(e) {
    e.preventDefault();
    try {
      const queued = await api('research/scripts', { method: 'POST', json: { series_id: series, sources: chosen, wiki } });
      setJobId(queued.job_id);
      setStatus('Queued. Sources that do not have this title are skipped quietly.');
    } catch (err) { setStatus(err.message); }
  }
  async function place(script, episode) {
    try {
      await api(`research/scripts/${script.id}/place`, { method: 'POST', json: { season: script.season, episode: episode === '' ? null : Number(episode) } });
      queryClient.invalidateQueries({ queryKey: scriptsKey(series) });
    } catch (err) { setStatus(err.message); }
  }
  const scripts = data?.scripts || [];
  return (
    <div className="research-block">
      {enabled && (
        <form className="research-row" onSubmit={find} id="researchScriptsFind">
          {SCRIPT_SOURCES.map(([name, label]) => (
            <label key={name} className="research-check">
              <input type="checkbox" checked={chosen.includes(name)}
                onChange={e => setChosen(e.target.checked ? [...chosen, name] : chosen.filter(n => n !== name))} /> {label}
            </label>
          ))}
          <input className="input research-wiki" value={wiki} onChange={e => setWiki(e.target.value.toLowerCase())}
            placeholder="wiki name (optional)" aria-label="Fandom wiki name" pattern="[a-z0-9-]*" />
          <button type="submit" className="btn btn-secondary" disabled={!chosen.length}>Find scripts</button>
        </form>
      )}
      <Status>{status}{job && job.status !== 'queued' ? ` ${job.status}: ${job.message || ''}` : ''}</Status>
      {scripts.length ? (
        <div className="research-table-wrap">
          <table className="table research-table" id="researchScripts">
            <thead><tr><th>{show ? 'Episode' : 'Part'}</th><th>Source</th><th>Kind</th><th>Lines</th><th>Page</th></tr></thead>
            <tbody>
              {scripts.map(s => (
                <tr key={s.id}>
                  <td>{show ? (
                    <input className="input research-episode" type="number" min="0" defaultValue={s.episode ?? ''}
                      aria-label="Episode number" onBlur={e => { if (String(s.episode ?? '') !== e.target.value) place(s, e.target.value); }} />
                  ) : '—'}</td>
                  <td><SourceBadge name={s.source.split(':')[0]} /> <span className="note">{s.source.split(':')[1] || ''}</span></td>
                  <td>{s.kind}{s.language ? ` · ${s.language}` : ''}</td>
                  <td>{s.lines}{s.speakers ? ` · ${s.speakers} speakers` : ''}</td>
                  <td>{/^https?:/.test(s.url) ? <a href={s.url} target="_blank" rel="noreferrer">{s.title || s.url}</a> : (s.title || s.url)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : <EmptyState>No reference scripts found yet. Most titles have none; that is normal.</EmptyState>}
    </div>
  );
}

function TitleIds({ series, enabled }) {
  const { data } = useQuery({ queryKey: titlesKey(series), queryFn: () => api(`research/titles?series_id=${encodeURIComponent(series)}`) });
  const [status, setStatus] = useState('');
  async function refresh() {
    setStatus('Looking the title up…');
    try {
      const info = await api('research/titles', { method: 'POST', json: { series_id: series } });
      queryClient.setQueryData(titlesKey(series), { ...data, info });
      const skipped = Object.entries(info.skipped || {}).map(([k, v]) => `${k}: ${v}`).join('; ');
      setStatus(skipped ? `Skipped ${skipped}` : 'Updated.');
    } catch (err) { setStatus(err.message); }
  }
  const ids = data?.info?.external_ids || data?.known || {};
  const aliases = data?.info?.aliases || [];
  return (
    <div className="research-block">
      <div className="research-ids" id="researchIds">
        {Object.entries(ids).map(([k, v]) => <span key={k} className="tag tag-neutral">{k} {v}</span>)}
        {enabled && <button type="button" className="btn btn-ghost research-mini" onClick={refresh}>Look up ids and names</button>}
      </div>
      {aliases.length > 0 && (
        <p className="note research-aliases">{aliases.slice(0, 30).map(a => `${a.title}${a.language || a.region ? ` (${[a.language, a.region].filter(Boolean).join('-')})` : ''}`).join(' · ')}</p>
      )}
      <Status>{status}</Status>
    </div>
  );
}

export function ResearchTab({ item, targets }) {
  const series = seriesIdOf(item);
  const { data: catalogues } = useQuery({ queryKey: ['catalogues'], queryFn: () => api('published-cast/catalogues'), staleTime: Infinity });
  const { data: cast } = useQuery({ queryKey: castKey(series), enabled: Boolean(series),
    queryFn: () => api(`published-cast?series_id=${encodeURIComponent(series)}`) });
  if (!series) {
    return <div className="panel title-panel"><p className="note">Research needs this title&apos;s TVDB or TMDB id; this one has neither.</p></div>;
  }
  const enabled = Boolean(catalogues?.enabled);
  const linked = (cast?.sources || []).map(s => s.source);
  const target = (targets || [])[0] || 'es';
  return (
    <div className="meta-stack research-tab">
      {!enabled && (
        <div className="panel title-panel research-off" id="researchOff">
          <p className="title-panel-sub">Title research is off, so nothing here reaches the internet. What was found earlier is still shown.
            {' '}<Link to="/settings/research">Turn it on in Settings</Link>.</p>
        </div>
      )}
      <div className="panel title-panel">
        <h3 className="meta-heading">Published cast</h3>
        <p className="title-panel-sub meta-sub">Who the characters are and who voices them in each language, from public catalogues you link. Searching sends only the title.</p>
        <CastLinks series={series} cast={cast} enabled={enabled} />
        {enabled && catalogues && <CastSearch series={series} title={cast?.title || item.title} catalogues={catalogues.catalogues} linked={linked} />}
        <MergedCast cast={cast} target={target} />
      </div>
      <div className="panel title-panel">
        <h3 className="meta-heading">Questions about this title</h3>
        <p className="title-panel-sub meta-sub">A cited answer from the web: how the dub says names, who voices whom, what a term means. Answers become notes, terms and cast leads for you to review; none is applied on its own.</p>
        <Questions series={series} title={item.title} enabled={enabled} />
      </div>
      <div className="panel title-panel">
        <h3 className="meta-heading">Reference scripts</h3>
        <p className="title-panel-sub meta-sub">Transcripts, screenplays and subtitles other people wrote. Kept apart from what episodes taught; an analysis reads them only when that is turned on in Settings.</p>
        <Scripts series={series} enabled={enabled} show={isShow(item)} />
      </div>
      <div className="panel title-panel">
        <h3 className="meta-heading">Ids and other names</h3>
        <TitleIds series={series} enabled={enabled} />
      </div>
    </div>
  );
}
