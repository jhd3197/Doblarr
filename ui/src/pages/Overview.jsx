import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Link } from 'react-router';
import { api } from '../lib/api.js';
import { audioLanguagesQuery, ensure, hardwareQuery, jobsQuery, libraryQuery, queryClient, statusQuery } from '../lib/queries.js';
import { jobStatusClass, useJobs } from '../lib/jobs.js';
import { useLogLines } from '../lib/events.js';
import { hardwareSummary } from '../lib/hardware.js';
import { languageName } from '../lib/languages.js';
import { isShow, itemKey, titleHref } from '../lib/library.js';
import { Poster } from '../components/Poster.jsx';
import { queueDub } from './library/parts.jsx';
import { notify } from '../lib/notice.js';

export async function overviewLoader() {
  // Jobs, counts and the GPU card are quick; the library scan and the audio
  // track scan load inside their own sections, so a cold scan never holds the page.
  await Promise.all([ensure(jobsQuery), ensure(statusQuery).catch(() => null), ensure(hardwareQuery)]);
  return null;
}

const byRecent = (a, b) => (b.updated_at || '').localeCompare(a.updated_at || '');
const refreshJobs = () => queryClient.invalidateQueries({ queryKey: ['jobs'] });
const hhmm = j => (j.updated_at || '').slice(11, 16);
const target = j => j.target_locale || j.target_lang;
const plural = (n, one, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;
const RETRYABLE = new Set(['full', 'tease', 'audition', 'analyze']);

// "Shows · S01E02" jobs are titled after the show; the longest library title
// the job's title starts with is its item.
function itemFor(job, items) {
  const t = String(job.title || '').toLowerCase();
  return (items || []).filter(i => t.startsWith(String(i.title).toLowerCase()))
    .sort((a, b) => b.title.length - a.title.length)[0];
}

// Finished dubs with a file to watch, newest first.
const readyJobs = jobs => jobs.filter(j => j.status === 'done' && j.has_file && !String(j.kind || '').startsWith('studio_')).sort(byRecent);

// Failed jobs nobody has re-run since: no newer job for the same title and target.
function failedJobs(jobs) {
  const sorted = [...jobs].sort(byRecent);
  return sorted.filter((j, i) => j.status === 'failed'
    && !sorted.slice(0, i).some(n => n.title === j.title && target(n) === target(j)));
}

function headline(ready, needs) {
  if (!ready && !needs) return 'Nothing waiting on you';
  const parts = [];
  if (ready) parts.push(`${plural(ready, 'dub')} ready to hear`);
  if (needs) parts.push(`${plural(needs, 'thing')} ${needs === 1 ? 'needs' : 'need'} you`);
  return parts.join(', ');
}

function Card({ title, sub, link, children, className = '' }) {
  return (
    <section className={`panel ov-card ${className}`}>
      <div className="ov-card-head">
        <h3>{title}</h3>
        {sub && <span className="ov-card-sub">{sub}</span>}
        {link}
      </div>
      {children}
    </section>
  );
}

function ReadyRow({ job, item }) {
  const lang = target(job);
  return (
    <div className="ov-ready">
      <div className="ov-ready-poster">{item ? <Poster item={item} /> : <div className="poster-fallback" />}</div>
      <div className="ov-ready-body">
        {item && item.title !== job.title && <div className="ov-ready-show">{item.title}</div>}
        <div className="ov-ready-title">{job.title}</div>
        <div className="ov-tags">
          {lang && <span className="tag tag-accent">{languageName(lang)}</span>}
          {job.version_name && <span className="tag tag-neutral">{job.version_name}</span>}
          <span className="tag tag-neutral">{hhmm(job) || 'done'}</span>
        </div>
        {job.message && <div className="ov-muted ov-small">{job.message}</div>}
        <div className="ov-actions">
          <Link to={`/watch/${encodeURIComponent(job.id)}`} className="btn btn-primary">Watch &amp; compare</Link>
          {job.has_review && <Link to="/dubs" className="btn btn-secondary">Review{job.review_count ? ` (${job.review_count})` : ''}</Link>}
        </div>
      </div>
    </div>
  );
}

function Ready({ ready, items }) {
  return (
    <Card title="Ready to review" sub="Listen before it goes to Plex" link={<Link to="/dubs" className="ov-link">All dubs</Link>}>
      <div className="ov-stack" id="recentTitles">
        {ready.length ? ready.slice(0, 3).map(j => <ReadyRow key={j.id} job={j} item={itemFor(j, items)} />)
          : <div className="note">No finished dubs to watch yet.</div>}
      </div>
    </Card>
  );
}

function NeedRow({ dot, title, sub, action }) {
  return (
    <div className="ov-need">
      <span className={`ov-dot${dot ? ' ov-dot-accent' : ''}`} />
      <div className="ov-need-text"><div className="ov-need-title">{title}</div>{sub && <div className="ov-muted ov-small">{sub}</div>}</div>
      {action}
    </div>
  );
}

function RetryButton({ job }) {
  const [state, setState] = useState('Retry');
  async function retry() {
    setState('Queuing…');
    try {
      await api('jobs', { method: 'POST', json: {
        title: job.title, source: job.source || 'manual', source_lang: job.source_lang || 'auto',
        target_lang: target(job), path: job.input_file || null, kind: job.kind || 'full',
        overrides: job.overrides && Object.keys(job.overrides).length ? job.overrides : undefined,
      } });
      setState('Queued ✓');
      refreshJobs();
    } catch (err) {
      setState('Retry');
      notify('Could not queue it again: ' + err.message);
    }
  }
  return <button type="button" className="btn btn-secondary ov-btn-sm" disabled={state !== 'Retry'} onClick={retry}>{state}</button>;
}

async function resumeQueue() {
  try { await api('api/queue/resume', { method: 'POST' }); } catch (e) { notify(e.message); }
  refreshJobs();
}

function Needs({ failed, pausedQueued, warnings }) {
  const rows = [];
  if (pausedQueued) rows.push(<NeedRow key="paused" dot title={`Queue paused with ${plural(pausedQueued, 'job')} waiting`}
    sub="Nothing runs until the queue is resumed." action={<button type="button" className="btn btn-secondary ov-btn-sm" onClick={resumeQueue}>Resume</button>} />);
  for (const j of failed) rows.push(<NeedRow key={j.id} dot title={`Job failed — ${j.title}`}
    sub={[j.stage && `${j.stage} stage`, j.message].filter(Boolean).join(': ') || `${j.source_lang} → ${target(j)}`}
    action={j.input_file && RETRYABLE.has(j.kind || 'full') ? <RetryButton job={j} /> : <Link to="/dubs" className="btn btn-secondary ov-btn-sm">Details</Link>} />);
  for (const w of warnings) rows.push(<NeedRow key={w} title="Library source unavailable" sub={w}
    action={<Link to="/settings/connections" className="btn btn-secondary ov-btn-sm">Settings</Link>} />);
  return (
    <Card title="Needs you" sub={rows.length ? plural(rows.length, 'thing') : null}>
      <div className="ov-stack ov-stack-tight">{rows.length ? rows : <div className="note">Nothing needs you right now.</div>}</div>
    </Card>
  );
}

const TRACK_STATS = [['und', 'Tagged “und”', 'Language unknown to players'],
  ['mismatched_tag', 'Tag ≠ what’s heard', 'The tag names another language'],
  ['inconsistent_titles', 'Inconsistent titles', 'One language under several names'],
  ['wrong_default', 'Wrong default track', 'Default isn’t the original or a dub']];

// Hidden until the track scan answers; a library without Plex has no band.
function AudioBand() {
  const { data, isError } = useQuery({ ...audioLanguagesQuery, retry: false });
  const totals = data?.totals;
  if (isError || !totals?.tracks) return null;
  const tiles = TRACK_STATS.filter(([k]) => k === 'und' || k === 'inconsistent_titles' || totals[k] > 0);
  return (
    <section className="ov-band" aria-label="Audio tracks across your library">
      <div className="ov-band-head">
        <div className="ov-band-text">
          <div className="ov-band-kicker">Audio tracks across your library</div>
          <div className="ov-band-title">{totals.to_fix
            ? `${totals.to_fix.toLocaleString()} of ${totals.tracks.toLocaleString()} audio tracks are named inconsistently`
            : `All ${totals.tracks.toLocaleString()} audio tracks are named consistently`}</div>
        </div>
        <Link to="/library/audio" className="btn btn-primary">Review audio languages</Link>
      </div>
      {totals.to_fix > 0 && (
        <div className="ov-band-stats">
          {tiles.map(([k, label, ex]) => (
            <div key={k} className="ov-band-stat">
              <div className="ov-band-n">{(totals[k] || 0).toLocaleString()}</div>
              <div className="ov-band-k">{label}</div>
              <div className="ov-band-ex">{ex}</div>
            </div>
          ))}
        </div>
      )}
    </section>
  );
}

function ActiveJob({ job: j }) {
  return (
    <div className="ov-active">
      <div className="ov-active-head">
        <b className="ov-ellipsis">{j.title}</b>
        <span className="m ov-muted ov-small">{j.source_lang} → {target(j)}</span>
        <span className={`tag ${j.status === 'running' ? 'tag-accent' : 'tag-neutral'} ov-push`}>
          {j.status === 'running' ? (j.stage || 'running') : 'queued'}</span>
      </div>
      <div className="bar"><span style={{ width: `${j.progress || 0}%` }} /></div>
      <div className="m ov-muted ov-small">{j.progress || 0}%{j.status === 'running' && j.message ? ` — ${j.message}` : ''}</div>
    </div>
  );
}

function FlaggedRow({ item, on, toggle }) {
  const show = isShow(item);
  return (
    <label className="ov-flag">
      <input type="checkbox" checked={on} disabled={show} onChange={toggle} aria-label={`Queue ${item.title}`} />
      <span className="ov-ellipsis"><b>{item.title}</b>{item.year ? <span className="ov-muted"> {item.year}</span> : null}</span>
      {show ? <Link to={titleHref(item, 'episodes')} className="ov-small">Pick episodes</Link>
        : <span className="m ov-muted ov-small">{item.original && item.original !== '??' ? item.original : 'auto'}</span>}
    </label>
  );
}

function Queue({ active, library, libraryPending, libraryError }) {
  const [picked, setPicked] = useState([]);
  const [state, setState] = useState({ busy: false, text: '' });
  const flagged = (library?.items || []).filter(i => i.status === 'needs-dub');
  const shown = flagged.slice(0, 6);
  const selected = shown.filter(i => picked.includes(itemKey(i)));
  const toggle = key => setPicked(p => (p.includes(key) ? p.filter(k => k !== key) : [...p, key]));

  async function queue() {
    setState({ busy: true, text: '' });
    const failed = [];
    for (const item of selected) {
      try { await queueDub(item, 'full', library.target_languages); } catch (err) { failed.push(`${item.title}: ${err.message}`); }
    }
    setPicked([]);
    setState({ busy: false, text: failed.length ? failed.join('; ') : `Queued ${plural(selected.length, 'title')}.` });
  }

  let list;
  if (libraryError) list = <div className="note">The library scan is unavailable — check Settings › Connections.</div>;
  else if (libraryPending) list = <div className="note">Loading the library…</div>;
  else if (!flagged.length) list = <div className="note">No titles are flagged <span className="m">needs-dub</span>.</div>;
  else list = shown.map(i => <FlaggedRow key={itemKey(i)} item={i} on={picked.includes(itemKey(i))} toggle={() => toggle(itemKey(i))} />);

  return (
    <Card title="Queue" sub={active.length ? `${active.filter(j => j.status === 'running').length} running · ${active.filter(j => j.status === 'queued').length} waiting` : 'Empty'}
      link={<Link to="/library" className="ov-link">Library</Link>} className="ov-queue">
      <div id="inProgress" className="ov-stack ov-stack-tight">
        {active.length ? active.map(j => <ActiveJob key={j.id} job={j} />)
          : <div className="ov-muted ov-small">Nothing is running. Pick flagged titles to start — they run one at a time.</div>}
      </div>
      <div className="ov-flags">{list}</div>
      {flagged.length > 0 && (
        <div className="ov-actions ov-queue-foot">
          <button type="button" className="btn btn-primary" disabled={!selected.length || state.busy} onClick={queue}>
            {state.busy ? 'Queuing…' : selected.length ? `Queue ${selected.length} selected` : 'Select titles'}</button>
          {state.text && <span className="ov-muted ov-small" role="status">{state.text}</span>}
          {flagged.length > shown.length && <Link to="/library" className="ov-small ov-push">See all {flagged.length} flagged</Link>}
        </div>
      )}
    </Card>
  );
}

function Worker({ hw, counts, paused }) {
  const gpu = hardwareSummary(hw, hw?.memory);
  const state = paused ? 'paused' : counts.running ? 'processing' : 'idle';
  const tiles = [['Device', gpu.device], ['GPU memory', gpu.memory || '—'], ['Load', gpu.percent == null ? '—' : `${gpu.percent}%`]];
  return (
    <Card title="Worker" link={<span className={`tag ${state === 'processing' ? 'tag-accent' : state === 'paused' ? 'tag-outline' : 'tag-neutral'} ov-push`}>{state}</span>}>
      <div className="ov-tiles" title={[gpu.label, ...(hw?.notes || [])].join('\n')}>
        {tiles.map(([k, v]) => <div key={k} className="ov-tile"><div className="ov-muted ov-tiny">{k}</div><div className="ov-tile-v">{v}</div></div>)}
      </div>
      <div className="ov-muted ov-small">{gpu.label}{counts.queued ? ` · ${plural(counts.queued, 'job')} queued` : ''}. <Link to="/settings/hardware">Hardware settings</Link></div>
    </Card>
  );
}

const EVENT = { done: 'Dub completed', failed: 'Job failed', running: 'Processing', queued: 'Queued', cancelled: 'Cancelled' };

function Activity({ jobs }) {
  const recent = [...jobs].sort(byRecent).slice(0, 6);
  return (
    <Card title="Recent activity" link={<Link to="/dubs" className="ov-link">Jobs</Link>}>
      <div id="activityBody">
        {recent.length ? recent.map(j => (
          <div key={j.id} className="ov-act">
            <span className="m ov-muted ov-small">{hhmm(j)}</span>
            <span className={`ov-dot${j.status === 'failed' || j.status === 'running' ? ' ov-dot-accent' : ' ov-dot-ink'}`} />
            <div className="ov-act-text">
              <div className="ov-ellipsis"><b>{EVENT[j.status] || j.status}</b> · {j.title}</div>
              <div className="ov-muted ov-small ov-ellipsis">{[`${j.source_lang} → ${target(j)}`, j.version_name, j.message].filter(Boolean).join(' · ')}</div>
            </div>
            <span className={`tag ${jobStatusClass(j.status)}`}>{j.status}</span>
          </div>
        )) : <div className="note">No activity yet.</div>}
      </div>
    </Card>
  );
}

function LogPanel() {
  const lines = useLogLines();
  return (
    <details className="panel log-panel">
      <summary>Logs <span className="m log-sub">live from the server (last 200 lines)</span></summary>
      <pre id="logBox" className="m log-box" ref={el => { if (el) el.scrollTop = el.scrollHeight; }}>{lines.join('\n')}</pre>
    </details>
  );
}

export function Overview() {
  const { data } = useJobs();
  const { data: status } = useQuery(statusQuery);
  const { data: hw } = useQuery(hardwareQuery);
  const { data: library, isPending: libraryPending, isError: libraryError } = useQuery(libraryQuery);
  const jobs = data?.jobs || [];
  const counts = data?.counts || {};
  const paused = !!data?.paused;
  const ready = readyJobs(jobs);
  const failed = failedJobs(jobs).slice(0, 4);
  const active = jobs.filter(j => j.status === 'running' || j.status === 'queued')
    .sort((a, b) => (a.status === 'running' ? -1 : 0) - (b.status === 'running' ? -1 : 0));
  const pausedQueued = paused ? counts.queued || 0 : 0;
  const warnings = library?.warnings || [];
  const needs = failed.length + (pausedQueued ? 1 : 0) + warnings.length;
  const needsDub = status?.counts?.needs_dub ?? library?.counts?.needs_dub;
  const worker = paused ? 'The queue is paused.' : counts.running ? `The worker is processing ${plural(counts.running, 'job')}.` : 'The worker is idle.';
  const today = new Date().toLocaleDateString(undefined, { weekday: 'long', month: 'long', day: 'numeric' });

  return (
    <div className="page ov">
      <section className="ov-hero">
        <div className="ov-hero-text">
          <div className="ov-muted ov-date">{today}</div>
          <h1 className="ov-h1">{headline(ready.length, needs)}</h1>
          <div className="ov-muted ov-hero-sub">{worker}
            {needsDub != null && <> <span id="statNeedsDub">{needsDub}</span> {needsDub === 1 ? 'title' : 'titles'} in your library {needsDub === 1 ? 'is' : 'are'} flagged <code className="m">needs-dub</code>.</>}
          </div>
        </div>
        <div className="ov-actions">
          {ready[0] && <Link to={`/watch/${encodeURIComponent(ready[0].id)}`} className="btn btn-secondary">Review next dub</Link>}
          <Link to="/library" className="btn btn-primary">Queue from the library</Link>
        </div>
      </section>

      <div className="ov-grid">
        <Ready ready={ready} items={library?.items} />
        <Needs failed={failed} pausedQueued={pausedQueued} warnings={warnings} />
      </div>

      <AudioBand />

      <div className="ov-grid">
        <Queue active={active} library={library} libraryPending={libraryPending} libraryError={libraryError} />
        <Worker hw={hw} counts={counts} paused={paused} />
      </div>

      <Activity jobs={jobs} />
      <LogPanel />
    </div>
  );
}
