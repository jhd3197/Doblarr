import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Link, useLoaderData, useParams } from 'react-router';
import { api, apiUrl } from '../../lib/api.js';
import { safeGet } from '../../lib/storage.js';
import { useOpenStudio } from '../../lib/studio.js';
import { formatTime } from '../../lib/media.js';

// One finished dub, watched in the browser (doblarr/watch.py): every audio
// track one click away at the same moment, the original's subtitles, the
// places where the dub is clearly quieter than the original, and notes kept
// with the dub. "O" flips between the dub and the track it is compared with
// at the same time, "N" starts a note at the current moment, J / K step
// through the quieter spots. When the run kept a review snapshot its lines
// fill a lane on the timeline and a tab in the side panel.

const CATEGORIES = [['', 'Kind'], ['lost sound', 'Sound lost'], ['overlap', 'Voices overlap'],
  ['timing', 'Timing'], ['voice', 'Wrong or odd voice'], ['translation', 'Translation'],
  ['level', 'Too loud or quiet'], ['other', 'Other']];
const NO_LOUDNESS = { spans: [] };
const SEVERITIES = [['minor', 'Minor'], ['noticeable', 'Noticeable'], ['major', 'Major']];
const IN_ORIGINAL = [['no', 'Not in original'], ['yes', 'Also in original'], ['not sure', 'Not sure']];
const RESOLUTIONS = ['open', 'fixed', "won't fix"];
const FILTERS = [['all', 'All', 0], ['big', '≥ 15 dB', 15], ['huge', '≥ 25 dB', 25]];
const PAGE = 40;

const fetchWatch = (id, retry = false) => api(`watch/${encodeURIComponent(id)}${retry ? '?retry=true' : ''}`);
const band = db => (db >= 25 ? 'q-huge' : db >= 15 ? 'q-big' : 'q-small');

export async function watchLoader({ params }) {
  try { return { data: await fetchWatch(params.id), error: '' }; }
  catch (error) { return { data: null, error: error.message }; }
}

function label(track) {
  if (track.ours) return `${track.title || 'Dub'} (Doblarr)`;
  return track.title || track.lang || `Track ${track.stream}`;
}

const short = track => (track.ours ? 'Dub' : (track.lang || '').toUpperCase() || `Track ${track.stream}`);

function key() {
  const value = safeGet('doblarr_api_key', '');
  return value ? `?api_key=${encodeURIComponent(value)}` : '';
}

// The review snapshot's lines, if this run kept one; never holds the page.
function useLines(job) {
  const [lines, setLines] = useState([]);
  useEffect(() => {
    if (!job.has_review) return undefined;
    let live = true;
    api(`jobs/${encodeURIComponent(job.id)}/review`)
      .then(r => live && setLines((r.segments || []).map(s => ({ start: s.start, end: s.end,
        who: s.speaker || '', dub: s.text_translated || s.text_src || '', src: s.text_src || '' }))))
      .catch(() => {});
    return () => { live = false; };
  }, [job.id, job.has_review]);
  return lines;
}

function Pill({ on, dark, children, ...rest }) {
  return <button type="button" className={`watch-pill${dark ? ' watch-pill-dark' : ''}`} aria-pressed={on ? 'true' : 'false'} {...rest}>{children}</button>;
}

function Player({ jobId, data, reload }) {
  const ours = data.tracks.find(t => t.ours) || data.tracks[0];
  const original = data.tracks.find(t => t.original) || data.tracks.find(t => t !== ours);
  const [current, setCurrent] = useState(ours?.stream);
  const [compare, setCompare] = useState(original?.stream);
  const [length, setLength] = useState(() =>
    data.loudness?.curves?.ours?.length * (data.loudness?.window || 0.25) || 1);
  const [playhead, setPlayhead] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [at, setAt] = useState('0:00');
  const [subtitle, setSubtitle] = useState(-1);
  const [panel, setPanel] = useState('quiet');
  const [filter, setFilter] = useState('all');
  const [limit, setLimit] = useState(PAGE);
  const [severity, setSeverity] = useState('noticeable');
  const [inOriginal, setInOriginal] = useState('');
  const video = useRef(null);
  const stage = useRef(null);
  const lanes = useRef(null);
  const form = useRef(null);
  const resume = useRef(null);     // where to pick up after a language switch
  const openStudio = useOpenStudio();
  const lines = useLines(data.job);
  const loud = data.loudness || NO_LOUDNESS;
  const src = stream => `${apiUrl(`watch/${encodeURIComponent(jobId)}/audio/${stream}.mp4`)}${key()}`;
  const byStream = stream => data.tracks.find(t => t.stream === stream);
  const reference = byStream(compare);
  const minDb = FILTERS.find(f => f[0] === filter)[2];
  // Stable between renders: the key handler below depends on it, and the
  // playhead re-renders this page several times a second.
  const spans = useMemo(() => loud.spans.filter(s => s.quieter_db >= minDb), [loud.spans, minDb]);

  const switchTo = useCallback(stream => {
    if (stream === current) return;
    const v = video.current;
    resume.current = { time: v.currentTime, playing: !v.paused };
    setCurrent(stream);
  }, [current]);

  const compareWith = stream => { setCompare(stream); switchTo(stream); };

  function onMetadata() {
    const v = video.current;
    if (v.duration) setLength(v.duration);
    if (resume.current) {
      v.currentTime = resume.current.time;
      if (resume.current.playing) v.play().catch(() => {});
      resume.current = null;
    }
  }

  function onTime() {
    const v = video.current;
    setPlayhead(v.currentTime);
    if (document.activeElement?.name !== 'note') setAt(formatTime(v.currentTime));
  }

  const seek = useCallback((time, play = false) => {
    const v = video.current;
    v.currentTime = Math.max(0, Math.min(v.duration || Infinity, time));
    setPlayhead(v.currentTime);
    if (play) v.play().catch(() => {});
  }, []);

  const toggle = useCallback(() => {
    const v = video.current;
    if (v.paused) v.play().catch(() => {}); else v.pause();
  }, []);

  useEffect(() => {
    [...(video.current?.textTracks || [])].forEach((t, i) => { t.mode = i === subtitle ? 'showing' : 'disabled'; });
  }, [subtitle, current]);

  useEffect(() => {
    const step = dir => {
      const t = video.current.currentTime;
      // Each jump lands a second early, so "next" skips the spot just reached.
      const next = dir > 0 ? spans.find(s => s.start > t + 1.5) : spans.findLast(s => s.start < t - 0.5);
      if (next) seek(next.start - 1);
    };
    const onKey = e => {
      if (e.target.closest?.('input, select, textarea') || e.ctrlKey || e.metaKey || e.altKey) return;
      const k = e.key.toLowerCase();
      if (k === ' ') { e.preventDefault(); toggle(); }
      else if (k === 'o') {
        if (reference && ours) switchTo(current === ours.stream ? reference.stream : ours.stream);
      } else if (k === 'n') {
        e.preventDefault();
        video.current.pause();
        setAt(formatTime(video.current.currentTime));
        form.current.elements.note.focus();
      } else if (k === 'j' || k === 'k') step(k === 'k' ? 1 : -1);
      else if (e.key === 'ArrowLeft') { e.preventDefault(); seek(video.current.currentTime - 5); }
      else if (e.key === 'ArrowRight') { e.preventDefault(); seek(video.current.currentTime + 5); }
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [current, ours, reference, spans, switchTo, seek, toggle]);

  async function keepTime(action) {
    const time = video.current.currentTime;
    try {
      await action();
      await reload();
      if (video.current) video.current.currentTime = time;
    } catch (error) { window.alert(error.message); }
  }

  function saveNote(e) {
    e.preventDefault();
    const fields = Object.fromEntries(new FormData(form.current));
    const track = byStream(current);
    keepTime(async () => {
      await api(`watch/${encodeURIComponent(jobId)}/notes`, { method: 'POST', json: {
        ...fields, severity, in_original: inOriginal,
        at: Math.round(video.current.currentTime * 100) / 100, track: track ? label(track) : '' } });
      form.current.elements.note.value = '';
      form.current.elements.note.blur();
      setPanel('notes');
    });
  }

  const resolve = (note, resolution) => keepTime(() => api(
    `watch/${encodeURIComponent(jobId)}/notes/${encodeURIComponent(note.id)}`,
    { method: 'PATCH', json: { resolution, base_revision: Number(note.revision) } }));

  function onLanes(e) {
    const r = lanes.current.getBoundingClientRect();
    seek((e.clientX - r.left) / r.width * length);
  }

  const pct = t => `${t / length * 100}%`;
  const mark = (left, width) => ({ left: pct(left), width: `${Math.max(0.12, width / length * 100)}%` });
  const hearing = byStream(current);
  const now = lines.find(l => playhead >= l.start && playhead < l.end);
  const last = now || lines.findLast(l => l.start <= playhead) || lines[0];
  const near = spans.findIndex(s => Math.abs(s.start - playhead) < 1.6);
  const job = data.job;
  const tabs = [['quiet', 'Quieter spots', loud.spans.length], ['notes', 'Notes', data.notes.length],
    ...(lines.length ? [['lines', 'Lines', '']] : [])];

  return (
    <>
      <header className="watch-head">
        <div className="watch-titles">
          <nav className="watch-crumbs" aria-label="Breadcrumb">
            <Link to="/dubs">Dubs</Link><span aria-hidden="true">›</span><span>{job.title}</span>
            <span aria-hidden="true">›</span><strong>Watch dub</strong>
          </nav>
          <h1>{job.title}{job.version && <span className="watch-version"> · {job.version}</span>}</h1>
          <p className="hint">{job.output}</p>
        </div>
        {job.input_file && !String(job.kind || '').startsWith('studio_') && (
          <button type="button" className="btn btn-secondary"
            onClick={() => openStudio({ path: job.input_file, title: job.title, jobId: job.id })}>Open in studio</button>
        )}
      </header>

      <div className="watch-langs" role="group" aria-label="Audio language">
        <span className="watch-langs-label">Dub</span>
        {ours && <Pill on={current === ours.stream} data-stream={ours.stream} onClick={() => switchTo(ours.stream)}>{label(ours)}</Pill>}
        {data.tracks.length > 1 && <><span className="watch-divider" /><span className="watch-langs-label">Compare with</span></>}
        {data.tracks.filter(t => t !== ours).map(t => (
          <Pill key={t.stream} dark on={current === t.stream} data-stream={t.stream} data-compare={compare === t.stream ? 'true' : undefined}
            onClick={() => compareWith(t.stream)}>{label(t)}</Pill>
        ))}
      </div>

      <div className="watch-body">
        <div className="watch-main">
          <div className="watch-stage" ref={stage}>
            <video ref={video} className="watch-video" preload="metadata" src={src(current)} onClick={toggle}
              onLoadedMetadata={onMetadata} onTimeUpdate={onTime} onPlay={() => setPlaying(true)} onPause={() => setPlaying(false)}>
              {data.subtitles.map(s => (
                <track key={s.stream} kind="subtitles" label={s.title || s.lang} srcLang={s.lang}
                  src={`${apiUrl(`watch/${encodeURIComponent(jobId)}/subtitles/${s.stream}.vtt`)}${key()}`} />
              ))}
            </video>
            <span className={`watch-hearing${hearing?.ours ? ' watch-hearing-dub' : ''}`}>Hearing: {hearing ? (hearing.ours ? 'Dub' : label(hearing)) : '—'}</span>
            <div className="watch-controls">
              <button type="button" className="watch-play" aria-label={playing ? 'Pause' : 'Play'} onClick={toggle}>{playing ? '❚❚' : '▶'}</button>
              <span className="m watch-clock">{formatTime(playhead)} / {formatTime(length)}</span>
              {ours && reference && (
                <div className="watch-ab" role="group" aria-label="Dub or comparison">
                  <button type="button" aria-pressed={current === ours.stream ? 'true' : 'false'} onClick={() => switchTo(ours.stream)}>Dub</button>
                  <button type="button" aria-pressed={current === reference.stream ? 'true' : 'false'} onClick={() => switchTo(reference.stream)}>{short(reference)}</button>
                </div>
              )}
              {data.subtitles.length > 0 && (
                <select className="watch-subs" aria-label="Subtitles" value={subtitle} onChange={e => setSubtitle(Number(e.target.value))}>
                  <option value={-1}>Subtitles off</option>
                  {data.subtitles.map((s, i) => <option key={s.stream} value={i}>{s.title || s.lang}</option>)}
                </select>
              )}
              <span className="watch-keys">Space play · O switch · N note · J / K quieter spots · ← → 5 s</span>
              <button type="button" className="watch-full" aria-label="Full screen"
                onClick={() => (document.fullscreenElement ? document.exitFullscreen() : stage.current.requestFullscreen?.())}>⛶</button>
            </div>
          </div>

          <div className="watch-timeline">
            <div className="watch-lanes" ref={lanes} onClick={onLanes} role="img"
              aria-label="Where the dub is quieter than the original, and notes">
              {lines.length > 0 && (
                <div className="watch-lane">{lines.map(l => <span key={l.start} className="watch-tick" style={mark(l.start, l.end - l.start)} />)}</div>
              )}
              <div className="watch-lane">
                {loud.spans.map(s => (
                  <span key={`q${s.start}`} className={`watch-tick watch-quiet ${band(s.quieter_db)}`} style={mark(s.start, s.end - s.start)}
                    title={`${formatTime(s.start)}: ours ${s.quieter_db} dB quieter than the original`} />
                ))}
              </div>
              <div className="watch-lane">
                {data.notes.map((n, i) => (
                  <span key={`n${n.id}`} className={`watch-note-mark${n.resolution === 'open' ? '' : ' watch-note-done'}`}
                    style={{ left: pct(n.at) }} title={`${formatTime(n.at)}: ${n.note || n.category}`}>{i + 1}</span>
                ))}
              </div>
              <span className="watch-head-line" style={{ left: pct(playhead) }} />
            </div>
            <div className="watch-legend">
              {lines.length > 0 && <span><i className="watch-key-line" />Dubbed lines</span>}
              <span><i className="watch-key-quiet" />Quieter than {reference && !reference.ours ? label(reference) : 'the original'} (darker = bigger drop)</span>
              <span><i className="watch-key-note" />Your notes</span>
              <span className="m watch-legend-time">{formatTime(playhead)} / {formatTime(length)}</span>
            </div>
          </div>

          <form className="watch-note" ref={form} onSubmit={saveNote}>
            <span className="m watch-note-at">{at}</span>
            <select className="input" name="category" aria-label="What is wrong">
              {CATEGORIES.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
            </select>
            <div className="watch-pills" role="group" aria-label="How much it matters">
              {SEVERITIES.map(([v, l]) => <Pill key={v} dark on={severity === v} onClick={() => setSeverity(v)}>{l}</Pill>)}
            </div>
            <div className="watch-pills" role="group" aria-label="Is it like this in the original?">
              {IN_ORIGINAL.map(([v, l]) => <Pill key={v} dark on={inOriginal === v} onClick={() => setInOriginal(inOriginal === v ? '' : v)}>{l}</Pill>)}
            </div>
            <input className="input" name="note" placeholder="What you heard, e.g. the laugh is gone" aria-label="Note" maxLength={2000}
              onKeyDown={e => { if (e.key === 'Escape') e.currentTarget.blur(); }} />
            <button type="submit" className="btn btn-primary">Save note</button>
          </form>
        </div>

        <aside className="watch-panel">
          {last && (
            <div className="watch-now">
              <div className="watch-now-who">{now ? `Now · ${now.who || 'line'}` : `Last line · ${last.who || 'line'}`}</div>
              <div className="watch-now-dub">{last.dub}</div>
              {last.src !== last.dub && <div className="watch-now-src">{last.src}</div>}
            </div>
          )}
          <div className="watch-tabs" role="tablist">
            {tabs.map(([id, name, n]) => (
              <button key={id} type="button" role="tab" aria-selected={panel === id ? 'true' : 'false'} onClick={() => setPanel(id)}>
                {name}{n !== '' && <span className="watch-count">{n}</span>}</button>
            ))}
          </div>

          {panel === 'quiet' && (
            <>
              <p className="watch-panel-hint">Dub is at least 9 dB below {original ? label(original) : 'the original'}
                {loud.offset_db ? ` after taking out an overall ${loud.offset_db} dB` : ''}: a laugh or a sound nobody voiced, or the music dipping.</p>
              <div className="watch-pills watch-filters">
                {FILTERS.map(([id, name, db]) => (
                  <Pill key={id} dark on={filter === id} onClick={() => { setFilter(id); setLimit(PAGE); }}>
                    {name} {loud.spans.filter(s => s.quieter_db >= db).length}</Pill>
                ))}
              </div>
              <ul className="watch-list watch-quiet-list">
                {spans.slice(0, limit).map((s, i) => (
                  <li key={s.start}>
                    <button type="button" className={`watch-row${i === near ? ' watch-row-near' : ''}`} onClick={() => seek(s.start - 1, true)}>
                      <span className="m watch-time">{formatTime(s.start)}</span>
                      <span>{(s.end - s.start).toFixed(1)} s · {s.quieter_db} dB quieter</span>
                      <span className="watch-meter"><span className={band(s.quieter_db)} style={{ width: `${Math.min(100, s.quieter_db / 37 * 100)}%` }} /></span>
                    </button>
                  </li>
                ))}
              </ul>
              {spans.length > limit && <button type="button" className="btn btn-ghost watch-more" onClick={() => setLimit(limit + 60)}>Show {spans.length - limit} more</button>}
              {!spans.length && <p className="watch-panel-hint">Nothing this much quieter.</p>}
            </>
          )}

          {panel === 'notes' && (
            data.notes.length ? (
              <ul className="watch-list watch-notes">
                {data.notes.map((n, i) => (
                  <li key={n.id} className={n.resolution === 'open' ? '' : 'watch-done'}>
                    <span className="watch-num">{i + 1}</span>
                    <button type="button" className="watch-note-body" onClick={() => seek(n.at - 1, true)}>
                      <span><strong className="m">{formatTime(n.at)}</strong>{n.category && <> · {CATEGORIES.find(c => c[0] === n.category)?.[1] || n.category}</>}</span>
                      <span className="watch-note-text">{n.note || 'No description'}</span>
                      <span className="watch-note-tags">
                        <span className={`tag ${n.severity === 'major' ? 'tag-accent' : 'tag-neutral'}`}>{n.severity}</span>
                        {n.in_original && <span className="tag tag-neutral">in original: {n.in_original}</span>}
                        {n.track && <span className="tag tag-neutral">{n.track}</span>}
                      </span>
                    </button>
                    <select className="input" aria-label="State of this note" value={n.resolution} onChange={e => resolve(n, e.target.value)}>
                      {RESOLUTIONS.map(r => <option key={r} value={r}>{r}</option>)}
                    </select>
                  </li>
                ))}
              </ul>
            ) : <p className="watch-panel-hint">Nothing noted yet. Pause where something sounds wrong and press <b>N</b>.</p>
          )}

          {panel === 'lines' && (
            <ul className="watch-list watch-lines">
              {lines.map(l => (
                <li key={l.start}>
                  <button type="button" className={`watch-row watch-line${l === last ? ' watch-row-near' : ''}`} onClick={() => seek(l.start - 0.2)}>
                    <span className="m watch-time">{formatTime(l.start)}</span>
                    <span><span className="watch-line-who">{l.who}</span><span className="watch-line-dub">{l.dub}</span>
                      {l.src !== l.dub && <span className="watch-line-src">{l.src}</span>}</span>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </aside>
      </div>
    </>
  );
}

export function Watch() {
  const { id } = useParams();
  return <WatchPage key={id} id={id} />;
}

function WatchPage({ id }) {
  const [state, setState] = useState(useLoaderData());

  const reload = useCallback(async (retry = false) => {
    try { setState({ data: await fetchWatch(id, retry), error: '' }); }
    catch (error) { setState({ data: null, error: error.message }); }
  }, [id]);

  // The browser copy is made once per dub; poll until it is ready.
  const waiting = state.data && state.data.state !== 'ready' && !state.data.state.startsWith('failed');
  useEffect(() => {
    if (!waiting) return undefined;
    const timer = setTimeout(() => reload(), 3000);
    return () => clearTimeout(timer);
  }, [waiting, state, reload]);

  let body;
  if (state.error) body = <p className="hint">{state.error}</p>;
  else if (state.data.state !== 'ready') {
    const failed = state.data.state.startsWith('failed');
    body = (
      <div className="watch-wait"><h2>{state.data.job.title}</h2>
        <p className="hint">{failed ? state.data.state : 'Making a copy any browser can play (once per dub, about a minute)…'}</p>
        {failed && <button type="button" className="btn btn-secondary" onClick={() => reload(true)}>Try again</button>}
      </div>
    );
  } else body = <Player key={id} jobId={id} data={state.data} reload={reload} />;
  return <div id="watchRoot" className="watch">{body}</div>;
}
