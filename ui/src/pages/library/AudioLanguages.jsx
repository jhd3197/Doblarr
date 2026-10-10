import { useEffect, useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { api } from '../../lib/api.js';
import { audioLanguagesQuery, queryClient } from '../../lib/queries.js';
import { safeGet, safeSet } from '../../lib/storage.js';
import './audio-languages.css';

// Every audio track in the library grouped by the language it is
// (doblarr/audio_languages.py), and the tracks whose tag or title should
// change to match one naming style. The server writes what this page sends.

const STYLES = [['channels', 'English 5.1'], ['kind', 'English · Dub'], ['language', 'English']];
const STYLE_KEY = 'doblarr.trackStyle';
const UNKNOWN = 'und';

function trackTitle(name, v, style) {
  if (v.kind === 'Commentary') return 'Commentary';
  const ai = v.kind === 'AI' ? ' AI' : '';
  if (style === 'channels') return [name, v.channels].filter(Boolean).join(' ') + ai;
  if (style === 'kind') return v.kind ? `${name} · ${v.kind}` : name;
  return name + ai;
}

// What one variant becomes: its language (picked by hand for unknown ones), tag and title.
function proposal(bucket, v, style, picked, languages) {
  if (v.kind === 'Commentary') {
    const known = bucket.lang !== UNKNOWN;
    return { language: known ? bucket.lang : '', code: known ? bucket.code : v.tag, title: 'Commentary' };
  }
  const lang = bucket.lang === UNKNOWN ? picked : bucket.lang;
  if (!lang) return null;
  const entry = bucket.lang === UNKNOWN ? languages.find(l => l.id === lang) : { name: bucket.name, code: bucket.code };
  return { language: lang, code: entry.code, title: trackTitle(entry.name, v, style) };
}

const changes = (v, p) => !!p && (p.code !== v.tag || p.title !== v.title);

function Variant({ v, p, on, open, onToggle, onOpen, onPick, languages, unknown }) {
  const changing = changes(v, p);
  return (
    <div className={`al-variant${open ? ' al-open' : ''}`}>
      <div className="al-row">
        <input type="checkbox" aria-label={`Rename ${v.title || v.tag}`} checked={on} disabled={!changing} onChange={onToggle} />
        <div className="al-names">
          <div className="al-name">
            <code className={`al-tag${changing && p.code !== v.tag ? ' al-tag-changed' : ''}`}>{v.tag}</code>
            <span className={changing ? 'al-old' : ''}>{v.title || <em>no title</em>}</span>
            {v.mismatch && <span className="tag tag-outline al-flag">tag says otherwise</span>}
          </div>
          <div className="al-name">
            <span className="al-arrow" aria-hidden="true">→</span>
            {unknown && v.kind !== 'Commentary' ? (
              <select className="input al-pick" aria-label="What language this is" value={p?.language || ''} onChange={e => onPick(e.target.value)}>
                <option value="">Pick the language…</option>
                {languages.map(l => <option key={l.id} value={l.id}>{l.name}</option>)}
              </select>
            ) : null}
            {p && <><code className="al-tag al-tag-new">{p.code}</code><b>{p.title || <em>no title</em>}</b></>}
            {p && !changing && <span className="al-ok">already named this way</span>}
          </div>
        </div>
        <span className="al-count">{v.tracks.toLocaleString()}</span>
        <button type="button" className="btn btn-ghost al-where" aria-expanded={open} onClick={onOpen}>{open ? 'Hide' : 'Where'}</button>
      </div>
      {open && (
        <ul className="al-files">
          {v.where.map(w => <li key={w.label}>{w.label} <span className="al-muted">· {w.tracks} {w.tracks === 1 ? 'track' : 'tracks'}</span></li>)}
          {v.titles > v.where.length && <li className="al-muted">and {v.titles - v.where.length} more titles</li>}
        </ul>
      )}
    </div>
  );
}

function ApplyStatus({ job, dryRun }) {
  if (!job || job.state === 'idle') return null;
  if (job.state === 'running') return <p className="al-status" role="status">Rewriting {job.done} of {job.files} files…</p>;
  return (
    <div className="al-status" role="status">
      <p>{job.message}{job.state === 'done' && !dryRun ? ' Plex picks the new names up as it refreshes each title.' : ''}</p>
      {job.failed?.length > 0 && <ul>{job.failed.map(f => <li key={f.file}><b>{f.file}</b>: {f.error}</li>)}</ul>}
    </div>
  );
}

const APPLY_KEY = ['audio-languages', 'apply'];

export function AudioLanguages() {
  const { data, error, isPending, isFetching } = useQuery({ ...audioLanguagesQuery, retry: false });
  const [style, setStyle] = useState(() => safeGet(STYLE_KEY, 'channels'));
  const [selected, setSelected] = useState(null);
  const [off, setOff] = useState({});
  const [picks, setPicks] = useState({});
  const [open, setOpen] = useState(null);
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState('');

  useEffect(() => { safeSet(STYLE_KEY, style); }, [style]);

  // The rewrite's progress: polled while it runs (a poll never starts while
  // the last one is waiting). Starting a rewrite seeds it, which starts the polling.
  const progress = useQuery({ queryKey: APPLY_KEY, queryFn: () => api('audio-languages/apply'),
    refetchInterval: q => (q.state.data?.state === 'running' ? 1500 : false) });
  const job = progress.data || data?.apply;
  const running = job?.state === 'running';
  const lastState = useRef(job?.state);
  useEffect(() => {
    // Only the moment a rewrite finishes reloads the tracks: reading them asks Plex again.
    if (lastState.current === 'running' && job?.state !== 'running')
      queryClient.invalidateQueries({ queryKey: audioLanguagesQuery.queryKey });
    lastState.current = job?.state;
  }, [job?.state]);

  if (isPending) return <div className="page al-page"><p className="al-muted">Reading every audio track from Plex… the first time takes a while on a big library.</p></div>;
  if (error) return <div className="page al-page"><h1 className="al-title">Audio languages</h1><p className="al-muted">Audio tracks are read from Plex: {error.message}</p></div>;

  const languages = data.languages || [];
  const proposals = b => b.variants.map(v => ({ v, p: proposal(b, v, style, picks[v.id], languages) }));
  // An unknown track still waiting for its language counts as one to fix.
  const toFix = b => proposals(b).filter(({ v, p }) => !p || changes(v, p)).reduce((n, { v }) => n + v.tracks, 0);
  const bucket = data.buckets.find(b => b.lang === selected) || data.buckets[0];
  const rows = bucket ? proposals(bucket) : [];
  const picked = rows.filter(({ v, p }) => changes(v, p) && !off[v.id]);
  const pickedTracks = picked.reduce((n, { v }) => n + v.tracks, 0);

  async function apply() {
    setBusy(true); setProblem('');
    try {
      queryClient.setQueryData(APPLY_KEY, await api('audio-languages/apply', { method: 'POST', json: {
        scanned_at: data.scanned_at,
        changes: picked.map(({ v, p }) => ({ id: v.id, language: p.language, title: p.title })) } }));
    } catch (e) { setProblem(e.message); }
    setBusy(false);
  }

  return (
    <div className="page al-page">
      <div className="al-head">
        <h1 className="al-title">Audio languages</h1>
        <span className="al-muted">Name tracks as</span>
        <div className="al-styles" role="group" aria-label="Naming style">
          {STYLES.map(([id, example]) => (
            <button key={id} type="button" aria-pressed={style === id} onClick={() => setStyle(id)}>{example}</button>
          ))}
        </div>
        <button type="button" className="btn btn-secondary" disabled={isFetching}
          onClick={() => api('audio-languages?refresh=true').then(fresh => queryClient.setQueryData(audioLanguagesQuery.queryKey, fresh)).catch(e => setProblem(e.message))}>
          {isFetching ? 'Reading…' : 'Rescan'}</button>
      </div>
      <p className="al-sub">
        {data.totals.tracks.toLocaleString()} audio tracks, read {data.scanned_at.replace('T', ' ').slice(0, 16)}.
        {' '}{data.writer === 'mkvpropedit'
          ? 'MKV files are edited in place with mkvpropedit; other files are copied with ffmpeg and swapped in.'
          : 'mkvpropedit is not installed, so each file is copied with ffmpeg and swapped in once the copy checks out. Installing MKVToolNix makes this instant for MKV files.'}
      </p>
      {data.dry_run && <p className="al-dry">Dry run is on (Settings): normalizing only reports what it would change.</p>}
      <ApplyStatus job={job || data.apply} dryRun={data.dry_run} />
      {problem && <p className="al-problem" role="alert">{problem}</p>}

      {!data.buckets.length ? <p className="al-muted">Plex lists no audio tracks.</p> : (
        <div className="al-layout">
          <nav className="panel al-buckets" aria-label="Languages">
            {data.buckets.map(b => {
              const fix = toFix(b);
              return (
                <button key={b.lang} type="button" className="al-bucket" aria-current={b === bucket ? 'true' : undefined}
                  onClick={() => { setSelected(b.lang); setOpen(null); }}>
                  <span className="al-bucket-name">{b.name}</span>
                  <span className={fix ? 'al-fix' : 'al-muted'}>{fix ? `${fix.toLocaleString()} to fix` : '✓'}</span>
                  <span className="al-muted">{b.tracks.toLocaleString()} tracks · {b.variants.length} {b.variants.length === 1 ? 'name' : 'names'}</span>
                  <code className="al-muted">{b.code}</code>
                </button>
              );
            })}
          </nav>
          <section className="panel al-detail" aria-label={bucket.name}>
            <div className="al-detail-head">
              <div>
                <h2>{bucket.name}</h2>
                <p className="al-muted">{bucket.lang === UNKNOWN
                  ? 'Tagged "und" and no language in the title. Pick what each one is.'
                  : bucket.variants.length === 1 ? `${bucket.tracks.toLocaleString()} tracks, all under one name`
                    : `${bucket.tracks.toLocaleString()} tracks found under ${bucket.variants.length} different names`}</p>
              </div>
              {toFix(bucket) ? (
                <button type="button" className="btn btn-primary" disabled={!pickedTracks || busy || running} onClick={apply}>
                  {pickedTracks ? `Normalize ${pickedTracks.toLocaleString()} tracks` : 'Nothing selected'}</button>
              ) : <span className="tag tag-neutral">All normalized</span>}
            </div>
            <div className="al-cols" aria-hidden="true"><span /><span>Found as → becomes</span><span>Tracks</span><span /></div>
            {rows.map(({ v, p }) => (
              <Variant key={v.id} v={v} p={p} languages={languages} unknown={bucket.lang === UNKNOWN}
                on={changes(v, p) && !off[v.id]} open={open === v.id}
                onToggle={() => setOff(o => ({ ...o, [v.id]: !o[v.id] }))}
                onOpen={() => setOpen(open === v.id ? null : v.id)}
                onPick={lang => setPicks(o => ({ ...o, [v.id]: lang }))} />
            ))}
          </section>
        </div>
      )}
    </div>
  );
}
