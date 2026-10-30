import { useState } from 'react';
import { Link, useLoaderData, useNavigate } from 'react-router';
import { useQuery } from '@tanstack/react-query';
import { api } from '../../lib/api.js';
import { ensure, libraryQuery, queryClient } from '../../lib/queries.js';
import { useJobs } from '../../lib/jobs.js';
import { isShow, itemKey, titleHref } from '../../lib/library.js';
import { useSearch } from '../../shell/Shell.jsx';
import { Poster } from '../../components/Poster.jsx';
import { PageHeader } from '../../components/PageHeader.jsx';
import { LangChips, QueueButton, StatusTag } from './parts.jsx';
import './library.css';

export async function libraryLoader() {
  try {
    await ensure(libraryQuery);
    return { error: '' };
  } catch (err) {
    return { error: err.message };
  }
}

const FILTERS = ['all', 'needs-dub', 'partial', 'available'];
const SORTS = [['default', 'Default order'], ['recent', 'Recently active']];

function Pills({ id, options, value, onChange }) {
  return (
    <div className="opts" id={id}>
      {options.map(([key, label]) => (
        <button key={key} type="button" className="opt" aria-pressed={value === key ? 'true' : 'false'}
          onClick={() => onChange(key)}>{label}</button>
      ))}
    </div>
  );
}

// Preview, then apply, the "needs dub" labels in Plex.
function usePlexLabels() {
  const [status, setStatus] = useState(null);
  async function sync(apply) {
    setStatus({ text: apply ? 'Applying Plex labels…' : 'Previewing…' });
    try {
      const r = await api('api/plex/labels', { method: 'POST', json: { apply } });
      setStatus({ result: r, apply });
    } catch (err) {
      setStatus({ text: 'Plex labels: ' + err.message });
    }
  }
  let body = status?.text || '';
  if (status?.result) {
    const r = status.result;
    const unmatched = (r.unmatched || []).length;
    body = status.apply
      ? <>Labelled <b>{r.added}</b> as “{r.label}”, removed <b>{r.removed}</b> stale · matched {r.matched}
          {unmatched ? ` · ${unmatched} not found in Plex` : ''}{r.kometa_file ? ' · Kometa fragment written' : ''}</>
      : <>Preview: would label <b>{r.added}</b> as “{r.label}” (matched {r.matched}{unmatched ? `, ${unmatched} unmatched` : ''}),
          remove <b>{r.removed}</b> stale.{' '}
          <button type="button" className="btn btn-primary library-apply" id="applyLabels" onClick={() => sync(true)}>Apply</button></>;
  }
  return { button: <button type="button" className="btn btn-secondary library-sync" id="syncLabelsBtn" onClick={() => sync(false)}>Sync Plex labels</button>,
    status: <div id="labelStatus" className="library-status">{body}</div> };
}

function PosterCard({ item, targets }) {
  const navigate = useNavigate();
  return (
    <div className="poster-card library-card">
      <Poster item={item} />
      <div className="poster-body">
        <div className="library-card-title">
          {/* A real link that covers the card, so the whole card opens the title
              and keyboard and screen-reader users reach it like any link. */}
          <Link className="library-card-link" to={titleHref(item)}>{item.title}</Link>
          {item.year ? <> <span className="m library-card-year">{item.year}</span></> : ''}
        </div>
        <LangChips item={item} targets={targets} />
        <div className="library-card-foot">
          <StatusTag label={item.label} />
          <span className="m library-card-source">{(item.source || '').split(' ')[0]}</span>
        </div>
        {/* Every card gets all three actions — an AI track can be added even
            when the title already has the target language. */}
        <div className="library-card-actions">
          <QueueButton item={item} kind="full" targets={targets} label="Queue dub" className="btn btn-ghost queue-dub"
            onShow={isShow(item) ? () => navigate(titleHref(item, 'episodes')) : undefined} />
          <QueueButton item={item} kind="tease" targets={targets} label="Tease" className="btn btn-ghost tease-dub"
            onShow={isShow(item) ? () => navigate(titleHref(item, 'episodes')) : undefined} />
          <button type="button" className="btn btn-ghost cast-edit"
            onClick={() => navigate(titleHref(item, 'voices'))}>Cast</button>
        </div>
      </div>
    </div>
  );
}

export function Library() {
  const { error } = useLoaderData();
  const search = useSearch();
  const { data } = useQuery({ ...libraryQuery, enabled: !error });
  const { data: jobs } = useJobs();
  const [filter, setFilter] = useState('all');
  const [sort, setSort] = useState('default');
  const [scan, setScan] = useState({ busy: false, error: error || '' });
  const labels = usePlexLabels();

  async function rescan() {
    setScan({ busy: true, error: '' });
    try {
      queryClient.setQueryData(['library'], await api('library?refresh=true'));
      setScan({ busy: false, error: '' });
    } catch (err) {
      setScan({ busy: false, error: err.message });
    }
  }

  // Job activity per title, for the "Recently active" sort.
  const activity = {};
  for (const j of jobs?.jobs || []) {
    const t = j.updated_at || '';
    if (!activity[j.title] || activity[j.title] < t) activity[j.title] = t;
  }
  const targets = data?.target_languages || ['en'];
  const rows = (data?.items || []).filter(i =>
    (filter === 'all' || i.status === filter) && (!search || String(i.title).toLowerCase().includes(search)));
  if (sort === 'recent') rows.sort((a, b) => (activity[b.title] || '').localeCompare(activity[a.title] || ''));

  let grid;
  if (scan.busy) grid = <div className="empty">Rescanning Sonarr and Radarr…</div>;
  else if (scan.error) grid = <div className="empty">Library scan unavailable — is Doblarr running (doblarr serve) and Radarr reachable? {scan.error}</div>;
  else if (!rows.length) grid = <div className="empty">No titles match.</div>;
  else grid = rows.map(item => <PosterCard key={itemKey(item)} item={item} targets={targets} />);

  return (
    <div className="page">
      <PageHeader title="Library" hint="Films and shows from Radarr and Sonarr, and which still need a dubbed track." />
      <div className="library-toolbar">
        <Pills id="libraryFilters" options={FILTERS.map(f => [f, f === 'all' ? 'All' : f])} value={filter} onChange={setFilter} />
        <Pills id="librarySort" options={SORTS} value={sort} onChange={setSort} />
        {labels.button}
        <button type="button" className="btn btn-secondary" id="rescanBtn" onClick={rescan}>Rescan sources</button>
        <Link to="/library/audio" className="btn btn-ghost">Audio languages</Link>
      </div>
      {labels.status}
      <div id="libraryGrid" className="poster-grid">{grid}</div>
    </div>
  );
}
